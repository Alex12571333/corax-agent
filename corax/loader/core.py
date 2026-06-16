"""agent-core execution-kernel seam.

The mirror image of :mod:`corax.loader.capabilities`: where that loader pulls
**capabilities** from standalone SDK packages, this one wires the **execution
kernel** from ``agent-core``. ``agent-core`` is imported **lazily**, so the
scaffold (menu, config, built-ins) runs on a pure-stdlib install — the kernel
is simply unavailable, and the runtime degrades gracefully.

`CoreEngine` is the seam the runtime owns. It does two things:

* *introspect* — report whether ``agent-core`` is installed and which of the
  runtime's loaded capabilities are real ``agent_core.Capability`` instances
  (the only ones the kernel can execute);
* *run* — assemble a fresh, fully-wired kernel (registry, router, policy,
  session/state/task stores, event bus, tracer and the async ``Executor``),
  register the executable capabilities, start the worker loop, hand back a
  :class:`RunningCore`, then tear it all down. Building and running happen
  inside the caller's event loop via :meth:`CoreEngine.session`, so there is no
  cross-loop primitive to leak.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any, AsyncIterator, Iterable

from ..config import AgentConfig


class KernelInvocationError(RuntimeError):
    """A capability invoked through the kernel did not complete successfully."""


_DECLARATION_ATTRS = (
    "id", "name", "description", "version", "tags", "permission_level",
    "required_scopes", "risk_level", "side_effects", "input_schema", "output_schema",
)
_echo_wrapper_cache: dict[int, type] = {}


def _echo_wrapper_class(ac: Any) -> type:
    """Build (once) a Capability subclass that auto-echoes its result to state.

    The agent-core Executor only surfaces a capability's output through
    ``state_patch`` -> ``StateManager``; the Task itself carries no result. So a
    kernel-driven caller (:meth:`RunningCore.invoke`) can only read a result a
    capability chose to echo. Wrapping every capability with this adapter makes
    that echo *automatic and universal* -- any capability, current or future,
    third-party or ours, returns its payload (and, on failure, its error)
    through the core without changing the capability itself. Off unless the
    caller supplies a ``state_key``.
    """
    cached = _echo_wrapper_cache.get(id(ac))
    if cached is not None:
        return cached

    class _StateEchoCapability(ac.Capability):
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            for attr in _DECLARATION_ATTRS:
                setattr(self, attr, getattr(inner, attr))

        async def execute(self, request: Any) -> Any:
            result = await self._inner.execute(request)
            key = request.input.get("state_key")
            if isinstance(key, str) and key and not result.state_patch:
                if result.is_success:
                    result.state_patch = {key: result.payload}
                elif result.error is not None:
                    result.state_patch = {
                        key: {
                            "_error": result.error.message,
                            "_details": result.error.details,
                        }
                    }
            return result

        async def health_check(self) -> Any:
            return await self._inner.health_check()

    _echo_wrapper_cache[id(ac)] = _StateEchoCapability
    return _StateEchoCapability


def _as_pairs(capabilities: Any) -> list[tuple[str, Any]]:
    """Normalise a capability collection to ``(id, instance)`` pairs.

    Accepts a mapping, an iterable of ``(id, instance)`` tuples, or our own
    :class:`~corax.registry.Registry` (whose iteration yields entries with
    ``.id`` / ``.item``).
    """
    if capabilities is None:
        return []
    if hasattr(capabilities, "items"):
        return list(capabilities.items())
    pairs: list[tuple[str, Any]] = []
    for entry in capabilities:
        if isinstance(entry, tuple):
            pairs.append(entry)
        else:
            pairs.append((getattr(entry, "id"), getattr(entry, "item")))
    return pairs


class RunningCore:
    """Handle to a live, started ``agent-core`` kernel (bound to one event loop).

    Obtained from :meth:`CoreEngine.session`. Lets a caller push work straight
    onto the task store and wait for it to settle, without needing a planner.
    """

    def __init__(
        self,
        agent_core: Any,
        executor: Any,
        task_store: Any,
        capability_ids: list[str],
        state_manager: Any | None = None,
    ) -> None:
        self._ac = agent_core
        self.executor = executor
        self.task_store = task_store
        self.capability_ids = list(capability_ids)
        self.state_manager = state_manager

    async def get_state(self, session_id: str) -> Any:
        """Read a session's ephemeral state (where capabilities' ``state_patch``
        output lands), so a kernel-driven caller can retrieve results."""
        return await self.state_manager.get_state(session_id)

    async def submit_task(
        self,
        *,
        required_capability: str,
        input: dict | None = None,
        task_type: str = "generic",
        session_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """Enqueue a READY task for ``required_capability`` and return its id."""
        Task, TaskStatus = self._ac.Task, self._ac.TaskStatus
        task = Task(
            session_id=session_id or f"session-{uuid.uuid4().hex[:8]}",
            task_type=task_type,
            status=TaskStatus.READY,
            input=dict(input or {}),
            required_capability=required_capability,
            timeout_seconds=timeout_seconds,
        )
        await self.task_store.save(task)
        return task.task_id

    async def wait(self, task_id: str, *, timeout: float = 5.0, poll: float = 0.02) -> Any:
        """Block until ``task_id`` reaches a terminal status; return the Task."""
        TaskStatus = self._ac.TaskStatus
        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            task = await self.task_store.get(task_id)
            if task is not None and task.status in terminal:
                return task
            await asyncio.sleep(poll)
        raise TimeoutError(f"task {task_id} did not settle within {timeout}s")

    async def run_task(self, *, wait_timeout: float = 5.0, **submit_kwargs: Any) -> Any:
        """``submit_task`` + ``wait`` in one call; returns the final Task."""
        task_id = await self.submit_task(**submit_kwargs)
        return await self.wait(task_id, timeout=wait_timeout)

    async def invoke(
        self,
        capability_id: str,
        input: dict | None = None,
        *,
        session_id: str | None = None,
        state_key: str = "_invoke_output",
        task_type: str = "generic",
        wait_timeout: float = 60.0,
    ) -> dict:
        """The canonical *through-the-core* call: run a capability and get its payload.

        Runs ``capability_id`` as a kernel task (so the kernel's policy, schema
        validation and tracing all apply), then reads the capability's output
        back from session state. Capabilities echo their payload into
        ``state_patch`` when handed a ``state_key`` — the only channel the core
        exposes for returning data — so any capability that follows that
        convention round-trips here without bespoke wiring.

        Raises :class:`KernelInvocationError` if the task does not complete.
        """
        sid = session_id or f"inv-{uuid.uuid4().hex[:8]}"
        payload = dict(input or {})
        if state_key:
            payload["state_key"] = state_key
        task = await self.run_task(
            required_capability=capability_id,
            input=payload,
            session_id=sid,
            task_type=task_type,
            wait_timeout=wait_timeout,
        )
        echoed = None
        if state_key and self.state_manager is not None:
            state = await self.state_manager.get_state(sid)
            echoed = state.temporary_context.get(state_key)

        if task.status is not self._ac.TaskStatus.COMPLETED:
            detail = ""
            if isinstance(echoed, dict) and echoed.get("_error"):
                detail = f": {echoed['_error']}"
                if echoed.get("_details"):
                    detail += f" {echoed['_details']}"
            raise KernelInvocationError(
                f"capability {capability_id!r} task ended {task.status.value}{detail}"
            )
        return dict(echoed) if isinstance(echoed, dict) else {}


class CoreEngine:
    """The runtime's seam onto the ``agent-core`` execution kernel."""

    def __init__(self, config: AgentConfig, *, log: logging.Logger | None = None) -> None:
        self.config = config
        self.log = log or logging.getLogger("corax.core")
        self._ac: Any | None = None
        self._probed = False

    # -- introspection (lazy, loop-free) --------------------------------- #
    def probe(self) -> bool:
        """Import ``agent-core`` once; cache the result. Returns availability."""
        if not self._probed:
            try:
                import agent_core  # noqa: PLC0415 - lazy by design
            except ImportError:
                self._ac = None
            else:
                self._ac = agent_core
            self._probed = True
        return self._ac is not None

    @property
    def available(self) -> bool:
        return self.probe()

    def is_executable(self, item: Any) -> bool:
        """True if ``item`` is a real ``agent_core.Capability`` the kernel can run."""
        return self.probe() and isinstance(item, self._ac.Capability)

    def executable_ids(self, capabilities: Any) -> list[str]:
        """Ids in ``capabilities`` that the kernel can actually execute."""
        if not self.probe():
            return []
        return [cid for cid, item in _as_pairs(capabilities) if self.is_executable(item)]

    # -- running --------------------------------------------------------- #
    @contextlib.asynccontextmanager
    async def session(
        self,
        capabilities: Iterable[Any] = (),
        *,
        policy: Any | None = None,
    ) -> AsyncIterator[RunningCore]:
        """Build, start, yield and tear down a fresh kernel in the current loop.

        Only the real ``agent_core.Capability`` instances among ``capabilities``
        are registered; everything else (e.g. the built-in echo placeholder) is
        skipped. A custom ``policy`` (any ``agent_core.PolicyEngine``) may be
        injected; otherwise the conservative ``DefaultPolicyEngine`` is used.
        """
        if not self.probe():
            raise RuntimeError("agent-core is not installed; the execution kernel is unavailable")
        ac = self._ac

        registry = ac.CapabilityRegistry()
        sessions = ac.SessionManager()
        state = ac.StateManager()
        task_store = ac.InMemoryTaskStore()
        bus = ac.InMemoryEventBus()
        trace = ac.TraceManager()
        policy = policy if policy is not None else ac.DefaultPolicyEngine()
        router = ac.Router(registry)
        executor = ac.Executor(
            session_manager=sessions,
            task_store=task_store,
            state_manager=state,
            registry=registry,
            router=router,
            policy=policy,
            event_bus=bus,
            trace=trace,
            config=self._executor_config(ac),
        )

        adopted: list[str] = []
        echo_cls = _echo_wrapper_class(ac)
        for cap_id, item in _as_pairs(capabilities):
            if not isinstance(item, ac.Capability):
                continue
            try:
                # Register a wrapper that auto-echoes the result to session state,
                # so every capability's output round-trips through the core.
                await registry.register(echo_cls(item))
            except Exception as exc:  # noqa: BLE001 - one bad cap must not abort the kernel
                self.log.warning("core rejected capability '%s': %s", cap_id, exc)
            else:
                adopted.append(item.id)

        lifecycle = ac.LifecycleManager()
        for name, component in (
            ("trace", trace),
            ("registry", registry),
            ("sessions", sessions),
            ("state", state),
            ("tasks", task_store),
            ("bus", bus),
            ("executor", executor),
        ):
            await lifecycle.register(name, component)

        await lifecycle.start_all()
        self.log.info("agent-core kernel started: %d capability(ies) adopted", len(adopted))
        try:
            yield RunningCore(ac, executor, task_store, adopted, state)
        finally:
            await lifecycle.stop_all()
            self.log.debug("agent-core kernel stopped")

    # -- internals ------------------------------------------------------- #
    def _executor_config(self, ac: Any) -> Any:
        """Translate the config ``limits`` section into an ExecutorConfig."""
        lim = self.config.limits
        return ac.ExecutorConfig(
            max_concurrency=lim.max_parallel_tasks,
            max_plan_tasks=lim.max_plan_tasks,
            max_tasks_per_correlation=lim.max_tasks_per_correlation,
            default_timeout_seconds=float(lim.task_timeout_seconds),
            max_payload_size=lim.max_payload_mb * 1_000_000,
        )
