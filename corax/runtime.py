"""Typed Corax runtime composition root.

Every installable component is an extension, but extensions are partitioned by
role.  Only the ``tool`` registry is handed to the agent-core task executor.
Channels, models, memory and runtime services are invoked by the host through
their role-specific contracts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_core import (
    ChannelMessage,
    ExtensionKind,
    ExtensionRequest,
    MemoryQuery,
    MemoryRecord,
    ModelRequest,
    TraceRecord,
    TraceStage,
)

from .capabilities import EchoCapability
from .config import AgentConfig, ExtensionSpec, validate_config
from .connectors import TerminalConnector
from .loader import CoreEngine, ExtensionLoader
from .memory import NullMemory
from .planner import StubPlanner
from .registry import ExtensionCatalog

_BUILTIN_FACTORIES: dict[str, Callable[[], Any]] = {
    "stub": StubPlanner,
    "memory.none": NullMemory,
    "terminal": TerminalConnector,
    "echo": EchoCapability,
}

_TEMPORAL_CONTEXT_HEADER = "Trusted Corax runtime context for this model request:"
_CONTEXT_DEFAULT_OUTPUT_TOKENS = 4_096
_CONTEXT_HOST_RESERVE_TOKENS = 1_024


def _context_reserved_tokens(max_tokens: Any) -> int:
    output_tokens = (
        max_tokens
        if type(max_tokens) is int and max_tokens > 0
        else _CONTEXT_DEFAULT_OUTPUT_TOKENS
    )
    return output_tokens + _CONTEXT_HOST_RESERVE_TOKENS


def _local_now() -> datetime:
    zone_name = os.environ.get("TZ", "").removeprefix(":").strip()
    if zone_name:
        try:
            return datetime.now(ZoneInfo(zone_name))
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return datetime.now().astimezone()


def _temporal_system_message(now: datetime) -> dict[str, str]:
    """Build trusted, request-local time and current-information rules."""

    if now.tzinfo is None or now.utcoffset() is None:
        now = now.astimezone()
    zone = getattr(now.tzinfo, "key", None) or now.tzname() or "local"
    offset = now.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}" if offset else "unknown"
    return {
        "role": "system",
        "content": (
            f"{_TEMPORAL_CONTEXT_HEADER}\n"
            f"- Local date: {now.date().isoformat()}\n"
            f"- Local time: {now.time().isoformat(timespec='seconds')}\n"
            f"- Timezone: {zone} (UTC{offset})\n"
            "- Resolve relative dates such as today and tomorrow from this block.\n"
            "- For current/latest/recent outside-world facts or events, web search "
            "is required before answering. Fetch relevant result pages when the "
            "web.fetch tool is available, assert only sourced facts, and report "
            "source URLs plus publication/event dates. Never guess current facts; "
            "if live retrieval is unavailable or inconclusive, say so."
        ),
    }


@dataclass
class RuntimeStatus:
    """A serialisable snapshot of role activation and kernel state."""

    running: bool
    started_at: str | None
    uptime_seconds: float
    agent_name: str
    mode: str
    active_by_kind: dict[str, list[str]] = field(default_factory=dict)
    bindings: dict[str, str] = field(default_factory=dict)
    registry_counts: dict[str, int] = field(default_factory=dict)
    core_available: bool = False
    core_tools: list[str] = field(default_factory=list)
    security_mode: str = ""

    # Deprecated 0.1 status views.
    planner_active: str = ""
    memory_active: str = ""
    connectors_active: list[str] = field(default_factory=list)
    capabilities_enabled: list[str] = field(default_factory=list)

    @property
    def core_capabilities(self) -> list[str]:
        return self.core_tools

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "uptime_seconds": round(self.uptime_seconds, 3),
            "agent_name": self.agent_name,
            "mode": self.mode,
            "active_by_kind": self.active_by_kind,
            "bindings": self.bindings,
            "registry_counts": self.registry_counts,
            "core_available": self.core_available,
            "core_tools": self.core_tools,
            "security_mode": self.security_mode,
            "planner_active": self.planner_active,
            "memory_active": self.memory_active,
            "connectors_active": self.connectors_active,
            "capabilities_enabled": self.capabilities_enabled,
        }

    def render(self) -> str:
        rows = [
            f"  state          : {'RUNNING' if self.running else 'stopped'}",
            f"  agent / mode   : {self.agent_name} / {self.mode}",
            f"  started_at     : {self.started_at or '-'}",
            f"  uptime         : {self.uptime_seconds:.1f}s",
        ]
        for kind, extension_ids in sorted(self.active_by_kind.items()):
            rows.append(f"  {kind:<15}: {', '.join(extension_ids) or '-'}")
        rows.extend(
            [
                "  bindings       : "
                + ", ".join(f"{role}={value}" for role, value in self.bindings.items()),
                "  core (tools)   : "
                + (
                    f"ready — {', '.join(self.core_tools) or 'no tools'}"
                    if self.core_available
                    else "unavailable (agent-core not installed)"
                ),
                f"  security       : {self.security_mode or 'default core policy'}",
            ]
        )
        return "\n".join(rows)


class CoraxRuntime:
    """Load, validate, start and invoke typed extensions."""

    def __init__(
        self,
        config: AgentConfig,
        logger: logging.Logger | None = None,
        *,
        root_path: str | Path | None = None,
        workspace_path: str | Path | None = None,
        core_version: str = "0.2.0",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.log = logger or logging.getLogger("corax.runtime")
        self.root_path = Path(root_path or Path.cwd()).resolve()
        self.workspace_path = Path(
            workspace_path or self.root_path / config.runtime.workspace_path
        ).resolve()
        self.data_path = (self.root_path / config.runtime.data_path).resolve()
        self.core_version = core_version
        self._clock = clock or _local_now
        self.model_context_window_tokens: int | None = None
        self._model_bytes_per_token: dict[str, float] = {}

        self.extensions = ExtensionCatalog()
        self.tools = self.extensions.registry(ExtensionKind.TOOL)
        self.channels = self.extensions.registry(ExtensionKind.CHANNEL_CONNECTOR)
        self.models = self.extensions.registry(ExtensionKind.MODEL_PROVIDER)
        self.memories = self.extensions.registry(ExtensionKind.MEMORY_PROVIDER)
        self.policies = self.extensions.registry(ExtensionKind.POLICY_PROVIDER)
        self.services = self.extensions.registry(ExtensionKind.RUNTIME_SERVICE)
        self.storage = self.extensions.registry(ExtensionKind.STORAGE_PROVIDER)
        self.observability = self.extensions.registry(ExtensionKind.OBSERVABILITY)

        # 0.1 aliases. Crucially, ``capabilities`` aliases tools only.
        self.capabilities = self.tools
        self.connectors = self.channels
        self.providers = self.models
        self.memory = self.memories

        self.extension_loader = ExtensionLoader(
            root_path=self.root_path,
            workspace_path=self.workspace_path,
            core_version=self.core_version,
            log=self.log,
        )
        self.capability_loader = self.extension_loader
        self.core = CoreEngine(self.config, log=self.log)
        self._running = False
        self._started_at: datetime | None = None
        self._started_extensions: list[tuple[str, Any]] = []
        self._managed_environment: dict[str, str] = {}

    async def start(self) -> None:
        if self._running:
            return
        if self._started_extensions:
            await self.stop()
        starting: tuple[str, Any] | None = None
        try:
            self._apply_environment()
            self._populate_extensions()
            for entry in list(self.extensions):
                starting = (entry.id, entry.item)
                try:
                    await asyncio.wait_for(
                        entry.item.start(),
                        timeout=float(self.config.limits.task_timeout_seconds),
                    )
                except Exception as exc:  # noqa: BLE001 - isolate one bad plugin
                    self.log.warning(
                        "extension '%s' failed to start and was isolated: %s",
                        entry.id,
                        exc,
                    )
                    await self._stop_extension(entry.id, entry.item)
                    self.extensions.registry(entry.item.kind).unregister(entry.id)
                else:
                    self._started_extensions.append((entry.id, entry.item))
                starting = None
            self._wire_runtime_services()
            await self._dispatch_hooks(
                "runtime_start",
                {
                    "root_path": str(self.root_path),
                    "workspace_path": str(self.workspace_path),
                },
            )
            self._running = True
            self._started_at = datetime.now(timezone.utc)
            await self.record_observation(
                TraceStage.SESSION_ACQUIRED,
                correlation_id=f"runtime-{self._started_at.isoformat()}",
                metadata={"event": "runtime_start", "mode": self.config.agent.mode},
            )
            self.log.debug("runtime started: %s", self.extensions.active_by_kind())
        except BaseException:
            # Cancellation and host-level startup failures are transactional.
            # The item whose start() was interrupted is not in the started list.
            if starting is not None:
                await self._stop_extension(*starting)
            await self.stop()
            self.extensions.clear()
            self._running = False
            self._started_at = None
            raise

    async def stop(self) -> None:
        if not self._running and not self._started_extensions:
            return
        if self._running:
            await self.record_observation(
                TraceStage.RESULT_PUBLISHED,
                correlation_id=f"runtime-{datetime.now(timezone.utc).isoformat()}",
                metadata={"event": "runtime_stop"},
            )
            await self._dispatch_hooks("runtime_stop", {"root_path": str(self.root_path)})
        for extension_id, item in reversed(self._started_extensions):
            await self._stop_extension(extension_id, item)
        self._started_extensions.clear()
        self.extensions.clear()
        self._running = False
        self._started_at = None

    async def reload_config(self, config: AgentConfig | None = None) -> None:
        candidate = config or self.config
        errors = validate_config(candidate)
        if errors:
            raise ValueError("invalid Corax config:\n- " + "\n- ".join(errors))

        was_running = self._running or bool(self._started_extensions)
        previous = self.config
        await self.stop()
        self._install_config(candidate)
        try:
            if was_running:
                await self.start()
        except BaseException:
            # Keep the last known-good composition usable if activation of the
            # candidate fails or is cancelled.
            try:
                await self.stop()
            except BaseException as exc:  # noqa: BLE001 - preserve reload error
                self.log.warning("failed cleaning up rejected config: %s", exc)
            self._install_config(previous)
            if was_running:
                try:
                    await self.start()
                except BaseException as exc:  # noqa: BLE001 - preserve reload error
                    self.log.error("failed restoring previous config: %s", exc)
            raise

    async def _stop_extension(self, extension_id: str, item: Any) -> None:
        task = asyncio.current_task()
        cancellation_count = task.cancelling() if task is not None else 0
        try:
            await asyncio.wait_for(
                item.stop(),
                timeout=float(self.config.limits.task_timeout_seconds),
            )
        except asyncio.CancelledError as exc:
            # Propagate a new cancellation, but do not let an extension that
            # cancels its own stop() abort the rest of an existing rollback.
            if task is not None and task.cancelling() > cancellation_count:
                raise
            self.log.warning(
                "extension '%s' cancelled while stopping: %s",
                extension_id,
                exc,
            )
        except Exception as exc:  # noqa: BLE001 - cleanup must stay fail-soft
            self.log.warning("failed stopping extension '%s': %s", extension_id, exc)

    def _install_config(self, config: AgentConfig) -> None:
        self.config = config
        self.core.config = config
        self.workspace_path = (
            self.root_path / config.runtime.workspace_path
        ).resolve()
        self.data_path = (self.root_path / config.runtime.data_path).resolve()
        self.extension_loader.workspace_path = self.workspace_path

    async def status(self) -> RuntimeStatus:
        return self.snapshot()

    def snapshot(self) -> RuntimeStatus:
        uptime = 0.0
        started = None
        if self._started_at is not None:
            started = self._started_at.isoformat()
            uptime = (datetime.now(timezone.utc) - self._started_at).total_seconds()
        active = self.extensions.active_by_kind()
        bindings = dict(self.config.extensions.bindings)
        return RuntimeStatus(
            running=self._running,
            started_at=started,
            uptime_seconds=uptime,
            agent_name=self.config.agent.name,
            mode=self.config.agent.mode,
            active_by_kind=active,
            bindings=bindings,
            registry_counts={
                kind: len(self.extensions.registry(kind))
                for kind in active
            },
            core_available=self.core.available,
            core_tools=self.core.executable_ids(self.tools),
            security_mode=self.security_mode(),
            planner_active=bindings.get("planner", ""),
            memory_active=bindings.get("memory", ""),
            connectors_active=active.get("channel_connector", []),
            capabilities_enabled=active.get("tool", []),
        )

    @property
    def running(self) -> bool:
        return self._running

    async def execute(
        self,
        required_capability: str,
        *,
        input: dict | None = None,
        task_type: str = "generic",
        timeout: float = 5.0,
    ) -> Any:
        """Compatibility name for executing one LLM-callable tool."""
        async with self.core.session(
            self.tools,
            policy=self.active_policy(),
            observability=self.active_observability(),
        ) as kernel:
            return await kernel.run_task(
                required_capability=required_capability,
                input=input,
                task_type=task_type,
                wait_timeout=timeout,
            )

    def active_policy(self) -> Any | None:
        """Return the host-selected policy provider, if it loaded."""

        policy_id = self.config.extensions.bindings.get("policy", "")
        if policy_id and self.policies.has(policy_id):
            return self.policies.get(policy_id)
        return None

    def active_memory(self) -> Any | None:
        """Return the host-selected memory provider, if it loaded."""

        memory_id = self.config.extensions.bindings.get("memory", "")
        if memory_id and self.memories.has(memory_id):
            return self.memories.get(memory_id)
        return None

    def active_memory_loop(self) -> Any | None:
        """Return the selected host-only memory orchestration service."""

        service_id = self.config.extensions.bindings.get(
            "memory_loop", "memory.loop"
        )
        if service_id and self.services.has(service_id):
            return self.services.get(service_id)
        return None

    def active_state_store(self) -> Any | None:
        """Return the selected host-only checkpoint provider."""

        storage_id = self.config.extensions.bindings.get("state", "")
        if storage_id and self.storage.has(storage_id):
            return self.storage.get(storage_id)
        return None

    def active_context_manager(self) -> Any | None:
        """Return the selected host-only context compaction service."""

        service_id = self.config.extensions.bindings.get("context", "")
        if service_id and self.services.has(service_id):
            return self.services.get(service_id)
        return None

    def active_mcp_manager(self) -> Any | None:
        """Return the configured MCP connection manager."""

        service_id = self.config.extensions.bindings.get("mcp", "")
        if service_id and self.services.has(service_id):
            return self.services.get(service_id)
        return None

    def active_skills_runtime(self) -> Any | None:
        """Return the configured progressive Agent Skills service."""

        service_id = self.config.extensions.bindings.get("skills", "")
        if service_id and self.services.has(service_id):
            return self.services.get(service_id)
        return None

    def active_hooks_runtime(self) -> Any | None:
        """Return the configured consent-gated lifecycle hook service."""

        service_id = self.config.extensions.bindings.get("hooks", "")
        if service_id and self.services.has(service_id):
            return self.services.get(service_id)
        return None

    def active_subagent_orchestrator(self) -> Any | None:
        """Return the configured bounded subagent delegation service."""

        service_id = self.config.extensions.bindings.get("subagents", "")
        if service_id and self.services.has(service_id):
            return self.services.get(service_id)
        return None

    def active_sandbox_executor(self) -> Any | None:
        """Return the selected fail-closed process sandbox backend."""

        service_id = self.config.extensions.bindings.get("sandbox", "")
        if service_id and self.services.has(service_id):
            return self.services.get(service_id)
        return None

    def active_model_router(self) -> Any | None:
        """Return the provider-agnostic model router when loaded."""

        router_id = self.config.extensions.bindings.get("model_router", "")
        if router_id and self.models.has(router_id):
            return self.models.get(router_id)
        return None

    def active_generation_model_id(self) -> str:
        """Return the bound generator, or the first healthy local fallback."""

        configured = self.config.extensions.bindings.get("primary_model", "")
        if configured and self.models.has(configured):
            provider = self.models.get(configured)
            if callable(getattr(provider, "generate", None)):
                return configured
        for entry in self.models.list_all():
            if callable(getattr(entry.item, "generate", None)):
                return entry.id
        return ""

    def active_observability(self) -> Any | None:
        """Return the selected host-only trace/audit sink."""

        provider_id = self.config.extensions.bindings.get("observability", "")
        if provider_id and self.observability.has(provider_id):
            return self.observability.get(provider_id)
        return None

    async def record_observation(
        self,
        stage: TraceStage,
        *,
        correlation_id: str,
        session_id: str = "",
        capability_id: str | None = None,
        duration_ms: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Emit a host-level record without letting telemetry break execution."""

        provider = self.active_observability()
        if provider is None:
            return
        try:
            await provider.record(
                TraceRecord(
                    correlation_id=correlation_id,
                    session_id=session_id,
                    stage=stage,
                    message="",
                    capability_id=capability_id,
                    duration_ms=duration_ms,
                    metadata=dict(metadata or {}),
                )
            )
        except Exception as exc:  # noqa: BLE001 - telemetry is fail-soft
            self.log.debug("observability provider failed: %s", exc)

    async def _dispatch_hooks(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        subject: str = "",
    ) -> list[dict[str, Any]]:
        service = self.active_hooks_runtime()
        if service is None or not hasattr(service, "dispatch"):
            return []
        try:
            result = await service.dispatch(event, payload, subject=subject)
        except Exception as exc:  # noqa: BLE001 - hooks cannot crash the host
            self.log.debug("hook dispatch failed: %s", exc)
            return []
        return result if isinstance(result, list) else []

    async def augment_with_hooks(
        self,
        messages: tuple | list,
        *,
        prompt: str = "",
        session_id: str = "",
    ) -> list:
        original = list(messages)
        results = await self._dispatch_hooks(
            "pre_model_call",
            {
                "session_id": session_id,
                "prompt": prompt,
                "messages": original,
            },
        )
        contexts = [
            str(item.get("context", "")).strip()
            for item in results
            if isinstance(item, dict) and str(item.get("context", "")).strip()
        ]
        if not contexts:
            return original
        context = "\n\n".join(contexts)[:32_000]
        insert_at = len(original)
        for index in range(len(original) - 1, -1, -1):
            if isinstance(original[index], dict) and original[index].get("role") == "user":
                insert_at = index
                break
        original.insert(
            insert_at,
            {
                "role": "system",
                "content": (
                    "Operator-approved pre-model hook context follows:\n\n" + context
                ),
            },
        )
        return original

    async def augment_with_skills(
        self,
        messages: tuple | list,
        *,
        prompt: str = "",
        session_id: str = "",
    ) -> list:
        service = self.active_skills_runtime()
        original = list(messages)
        if service is None:
            return original
        try:
            result = await service.handle(
                ExtensionRequest(
                    operation="augment",
                    payload={"messages": original, "query": prompt},
                    session_id=session_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - optional skills are fail-soft
            self.log.debug("skill selection failed: %s", exc)
            return original
        augmented = (result.payload or {}).get("messages", [])
        return augmented if getattr(result, "is_success", False) and isinstance(
            augmented, list
        ) else original

    async def compact_messages(
        self,
        messages: tuple | list,
        *,
        session_id: str = "",
    ) -> list:
        compacted, _, _, overflow = await self._compact_messages_with_context(
            messages,
            session_id=session_id,
        )
        if overflow:
            raise RuntimeError(
                "prepared model context exceeds the active model window"
            )
        return compacted

    def set_model_context_window(self, value: int | None) -> None:
        """Set the provider-discovered prompt window used for preflight."""

        self.model_context_window_tokens = (
            value if type(value) is int and value > 0 else None
        )

    def _with_temporal_context(self, messages: tuple | list) -> list:
        prepared = list(messages)
        insert_at = 0
        while (
            insert_at < len(prepared)
            and isinstance(prepared[insert_at], dict)
            and prepared[insert_at].get("role") == "system"
        ):
            insert_at += 1
        prepared.insert(insert_at, _temporal_system_message(self._clock()))
        return prepared

    async def _compact_messages_with_context(
        self,
        messages: tuple | list,
        *,
        session_id: str = "",
        model_key: str = "",
        max_tokens: Any = None,
        prompt_overhead_bytes: int = 0,
    ) -> tuple[list, dict[str, Any] | None, int | None, bool]:
        manager = self.active_context_manager()
        original = list(messages)
        if manager is None:
            return original, None, None, False
        payload: dict[str, Any] = {"messages": original}
        if self.model_context_window_tokens is not None:
            payload.update(
                {
                    "context_window_tokens": self.model_context_window_tokens,
                    "reserved_tokens": _context_reserved_tokens(max_tokens),
                    "prompt_overhead_bytes": max(0, prompt_overhead_bytes),
                    "observed_bytes_per_token": self._model_bytes_per_token.get(
                        model_key
                    ),
                }
            )
        try:
            result = await manager.handle(
                ExtensionRequest(
                    operation="compact",
                    payload=payload,
                    session_id=session_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - legacy compaction is fail-soft
            self.log.debug("context compaction failed: %s", exc)
            if self.model_context_window_tokens is not None:
                raise RuntimeError("model context preflight failed") from exc
            return original, None, None, False
        result_payload = result.payload or {}
        compacted = result_payload.get("messages", [])
        if not getattr(result, "is_success", False) or not isinstance(
            compacted, list
        ):
            if self.model_context_window_tokens is not None:
                raise RuntimeError("model context preflight failed")
            return original, None, None, False
        limit = result_payload.get("budget_limit")
        unit = result_payload.get("budget_unit")
        used = result_payload.get(
            "bytes_after" if unit == "bytes" else "chars_after"
        )
        if type(limit) is not int or limit <= 0:
            try:
                limit = getattr(manager, "max_chars", None)
            except Exception:  # noqa: BLE001 - optional reporting is fail-soft
                limit = None
        if unit not in {"bytes", "chars"}:
            unit = "chars"
        context = (
            {
                "type": "context",
                "used": used,
                "limit": limit,
                "unit": unit,
                "scope": "prepared",
                "source": "host",
            }
            if type(used) is int
            and used >= 0
            and type(limit) is int
            and limit > 0
            else None
        )
        prompt_bytes = result_payload.get("prompt_bytes_after")
        return (
            compacted,
            context,
            prompt_bytes
            if type(prompt_bytes) is int and prompt_bytes > 0
            else None,
            result_payload.get("overflow") is True,
        )

    @staticmethod
    def _prompt_overhead_bytes(data: dict[str, Any]) -> int:
        fields = {
            key: data[key]
            for key in ("tools", "tool_choice")
            if key in data
        }
        return len(
            json.dumps(fields, ensure_ascii=False, default=str).encode("utf-8")
        )

    @staticmethod
    def _model_request(
        prompt: str,
        messages: tuple | list,
        data: dict[str, Any],
        session_id: str,
    ) -> ModelRequest:
        parameters = dict(data)
        prepared = list(messages)
        system = [
            message
            for message in prepared
            if isinstance(message, dict) and message.get("role") == "system"
        ]
        if system:
            merged = dict(system[0])
            merged["content"] = "\n\n".join(
                content
                if isinstance(content := message.get("content", ""), str)
                else json.dumps(content, ensure_ascii=False, default=str)
                for message in system
                if message.get("content", "") not in ("", None)
            )
            prepared = [
                merged,
                *[
                    message
                    for message in prepared
                    if not (
                        isinstance(message, dict)
                        and message.get("role") == "system"
                    )
                ],
            ]
        return ModelRequest(
            prompt=prompt,
            messages=tuple(prepared),
            model=parameters.pop("model", None),
            modalities=tuple(parameters.pop("modalities", ("text",))),
            parameters=parameters,
            session_id=session_id,
        )

    async def _prepare_model_request(
        self,
        provider: Any,
        *,
        prompt: str,
        messages: tuple | list,
        data: dict[str, Any],
        session_id: str,
        model_key: str,
    ) -> tuple[ModelRequest, dict[str, Any] | None, int | None]:
        request = self._model_request(prompt, messages, data, session_id)
        original_messages = request.messages
        retry_exact = False
        try:
            exact_context = await self._preflight_model_context(
                provider,
                request,
                None,
            )
        except RuntimeError:
            retry_exact = True
        else:
            if exact_context is not None:
                return request, exact_context, None

        messages, fallback, prompt_bytes, overflow = (
            await self._compact_messages_with_context(
                messages,
                session_id=session_id,
                model_key=model_key,
                max_tokens=data.get("max_tokens"),
                prompt_overhead_bytes=self._prompt_overhead_bytes(data),
            )
        )
        request = self._model_request(prompt, messages, data, session_id)
        if retry_exact:
            context = await self._preflight_model_context(
                provider,
                request,
                fallback,
                fallback_overflow=(
                    overflow
                    or fallback is None
                    or request.messages == original_messages
                ),
            )
        elif overflow:
            raise RuntimeError(
                "prepared model context exceeds the active model window"
            )
        else:
            context = fallback
        return request, context, prompt_bytes

    async def _preflight_model_context(
        self,
        provider: Any,
        request: ModelRequest,
        fallback: dict[str, Any] | None,
        *,
        fallback_overflow: bool = False,
    ) -> dict[str, Any] | None:
        def use_fallback() -> dict[str, Any] | None:
            if fallback_overflow:
                raise RuntimeError(
                    "prepared model context exceeds the active model window"
                )
            return fallback

        counter = getattr(provider, "count_tokens", None)
        window = self.model_context_window_tokens
        if not callable(counter) or window is None:
            return use_fallback()
        try:
            used = await counter(request)
        except Exception as exc:  # noqa: BLE001 - optional provider preflight
            self.log.debug("provider token preflight failed: %s", exc)
            return use_fallback()
        if used is None:
            self.log.debug("provider token preflight returned no count")
            return use_fallback()
        if type(used) is not int or used < 0:
            self.log.debug("provider token preflight returned an invalid count")
            return use_fallback()
        if (
            used + _context_reserved_tokens(request.parameters.get("max_tokens"))
            > window
        ):
            raise RuntimeError(
                "prepared model context exceeds the active model window"
            )
        return {
            "type": "context",
            "used": used,
            "limit": window,
            "unit": "tokens",
            "scope": "prompt",
            "source": "provider",
        }

    def _record_context_calibration(
        self,
        model_key: str,
        *,
        prompt_bytes: int | None,
        prompt_tokens: Any,
    ) -> None:
        if (
            not model_key
            or type(prompt_bytes) is not int
            or prompt_bytes <= 0
            or type(prompt_tokens) is not int
            or prompt_tokens <= 0
        ):
            return
        observed = prompt_bytes / prompt_tokens
        previous = self._model_bytes_per_token.get(model_key)
        self._model_bytes_per_token[model_key] = (
            min(previous, observed) if previous is not None else observed
        )

    @staticmethod
    def _result_prompt_tokens(result: Any) -> int | None:
        payload = getattr(result, "payload", None)
        raw = payload.get("raw") if isinstance(payload, dict) else None
        usage = raw.get("usage") if isinstance(raw, dict) else None
        value = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        return value if type(value) is int and value >= 0 else None

    async def memory_before_turn(
        self,
        text: str,
        *,
        session_id: str,
        scope: dict | None = None,
    ) -> dict[str, Any]:
        loop = self.active_memory_loop()
        if loop is None:
            return {"context": "", "records": [], "provider": ""}
        try:
            result = await loop.handle(
                ExtensionRequest(
                    operation="before_turn",
                    payload={"text": text, "scope": dict(scope or {})},
                    session_id=session_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - optional memory is fail-soft
            self.log.warning("memory before_turn failed: %s", exc)
            return {"context": "", "records": [], "provider": "", "degraded": True}
        if getattr(result, "is_success", False):
            return dict(result.payload or {})
        return {"context": "", "records": [], "provider": "", "degraded": True}

    async def memory_after_turn(
        self,
        user_text: str,
        assistant_text: str,
        *,
        session_id: str,
        scope: dict | None = None,
        explicit: bool | None = None,
    ) -> dict[str, Any]:
        loop = self.active_memory_loop()
        if loop is None:
            return {"stored": False, "reason": "memory loop unavailable"}
        try:
            result = await loop.handle(
                ExtensionRequest(
                    operation="after_turn",
                    payload={
                        "user_text": user_text,
                        "assistant_text": assistant_text,
                        "scope": dict(scope or {}),
                        "explicit": explicit,
                    },
                    session_id=session_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - optional memory is fail-soft
            self.log.warning("memory after_turn failed: %s", exc)
            return {"stored": False, "reason": "memory loop unavailable"}
        if getattr(result, "is_success", False):
            return dict(result.payload or {})
        return {"stored": False, "reason": "memory provider rejected write"}

    def security_mode(self) -> str:
        policy = self.active_policy()
        mode = getattr(policy, "mode", None)
        return getattr(mode, "value", str(mode or ""))

    def _wire_runtime_services(self) -> None:
        """Bind cross-role services after every extension has started."""

        loop = self.active_memory_loop()
        if loop is not None and hasattr(loop, "bind"):
            try:
                loop.bind(self.active_memory())
            except Exception as exc:  # noqa: BLE001 - optional integration
                self.log.warning("failed wiring memory loop: %s", exc)
        router = self.active_model_router()
        if router is not None and hasattr(router, "bind"):
            try:
                router.bind(
                    {
                        entry.id: entry.item
                        for entry in self.models.list_all()
                        if entry.id != getattr(router, "id", "")
                    }
                )
            except Exception as exc:  # noqa: BLE001 - optional integration
                self.log.warning("failed wiring model router: %s", exc)
        manager = self.active_mcp_manager()
        if manager is not None and hasattr(manager, "tool_proxies"):
            try:
                for proxy in manager.tool_proxies():
                    if self.extensions.has(proxy.id):
                        self.log.warning("MCP tool id collision: %s", proxy.id)
                        continue
                    self.tools.register(proxy.id, proxy)
            except Exception as exc:  # noqa: BLE001 - optional integration
                self.log.warning("failed wiring MCP tools: %s", exc)
        orchestrator = self.active_subagent_orchestrator()
        if orchestrator is not None:
            try:
                if hasattr(orchestrator, "bind"):
                    orchestrator.bind(self._run_subagent_model)
                if hasattr(orchestrator, "tool_proxy"):
                    proxy = orchestrator.tool_proxy()
                    if self.extensions.has(proxy.id):
                        self.log.warning("subagent tool id collision: %s", proxy.id)
                    else:
                        self.tools.register(proxy.id, proxy)
            except Exception as exc:  # noqa: BLE001 - optional integration
                self.log.warning("failed wiring subagents: %s", exc)
        sandbox = self.active_sandbox_executor()
        if self.tools.has("shell") and hasattr(
            self.tools.get("shell"), "bind_executor"
        ):
            try:
                self.tools.get("shell").bind_executor(sandbox)
            except Exception as exc:  # noqa: BLE001 - shell remains fail-closed
                self.log.warning("failed wiring shell sandbox: %s", exc)
        hooks = self.active_hooks_runtime()
        if hooks is not None and hasattr(hooks, "wrap_tool"):
            try:
                if (
                    not hasattr(hooks, "has_event")
                    or hooks.has_event("pre_tool_call")
                    or hooks.has_event("post_tool_call")
                ):
                    for entry in self.tools.list_all():
                        entry.item = hooks.wrap_tool(entry.item)
            except Exception as exc:  # noqa: BLE001 - optional integration
                self.log.warning("failed wiring tool hooks: %s", exc)

    async def _run_subagent_model(
        self,
        payload: dict[str, Any],
        *,
        session_id: str,
    ) -> Any:
        model_id = self.active_generation_model_id()
        if not model_id:
            raise RuntimeError("primary model provider is unavailable")
        return await self.invoke_extension(
            model_id,
            payload,
            session_id=session_id,
        )

    async def security_control(
        self,
        command: str | list[str],
        *,
        actor: str = "local",
        transport: str = "local",
    ) -> dict[str, Any]:
        """Dispatch a host-only security command to the selected provider."""

        policy = self.active_policy()
        if policy is None or not hasattr(policy, "control"):
            return {
                "ok": False,
                "message": "no active security policy provider",
                "mode": "",
            }
        result = await policy.control(command, actor=actor, transport=transport)
        if not isinstance(result, dict):
            raise TypeError("security policy control must return a dict")
        return result

    async def invoke_extension(
        self,
        extension_id: str,
        payload: dict | None = None,
        *,
        session_id: str = "",
    ) -> Any:
        """Invoke a host-level extension through its role contract.

        Tools intentionally are not handled here; they must cross the kernel
        boundary so schema validation, policy and tracing remain active.
        """
        item = self.extensions.get(extension_id)
        kind = item.kind
        data = dict(payload or {})
        operation = str(data.pop("operation", ""))
        if kind is ExtensionKind.TOOL:
            raise TypeError(
                f"{extension_id!r} is a tool; invoke it through agent-core"
            )
        if kind is ExtensionKind.MODEL_PROVIDER:
            prompt = str(data.pop("prompt", ""))
            messages = await self.augment_with_skills(
                tuple(data.pop("messages", ())),
                prompt=prompt,
                session_id=session_id,
            )
            messages = await self.augment_with_hooks(
                messages,
                prompt=prompt,
                session_id=session_id,
            )
            messages = self._with_temporal_context(messages)
            model_key = str(data.get("model") or extension_id)
            if operation == "plan":
                request = self._model_request(
                    prompt,
                    messages,
                    data,
                    session_id,
                )
                prompt_bytes = None
            else:
                request, _, prompt_bytes = await self._prepare_model_request(
                    item,
                    prompt=prompt,
                    messages=messages,
                    data=data,
                    session_id=session_id,
                    model_key=model_key,
                )
            correlation_id = getattr(request, "request_id", "") or f"model-{session_id}"
            await self.record_observation(
                TraceStage.PLANNER_CALLED,
                correlation_id=correlation_id,
                session_id=session_id,
                capability_id=extension_id,
                metadata={
                    "model": request.model or "",
                    "modalities": list(request.modalities),
                    "operation": operation or "generate",
                },
            )
            try:
                if operation == "plan" and hasattr(item, "plan"):
                    from agent_core import CapabilityRequest

                    result = await item.plan(
                        CapabilityRequest(
                            task_id=f"plan-{request.request_id if hasattr(request, 'request_id') else 'request'}",
                            session_id=session_id,
                            input={"goal": request.prompt, **request.parameters},
                        )
                    )
                else:
                    result = await item.generate(request)
                self._record_context_calibration(
                    model_key,
                    prompt_bytes=prompt_bytes,
                    prompt_tokens=self._result_prompt_tokens(result),
                )
            except Exception as exc:
                await self.record_observation(
                    TraceStage.ERROR,
                    correlation_id=correlation_id,
                    session_id=session_id,
                    capability_id=extension_id,
                    metadata={
                        "error_type": type(exc).__name__,
                        "operation": operation or "generate",
                    },
                )
                raise
            await self.record_observation(
                TraceStage.RESULT_PUBLISHED,
                correlation_id=correlation_id,
                session_id=session_id,
                capability_id=extension_id,
                metadata={
                    "status": getattr(
                        getattr(result, "status", None),
                        "value",
                        "",
                    ),
                    "operation": operation or "generate",
                },
            )
            await self._dispatch_hooks(
                "post_model_call",
                {
                    "session_id": session_id,
                    "model": request.model or "",
                    "status": getattr(getattr(result, "status", None), "value", ""),
                    "output": getattr(result, "payload", None),
                },
            )
        elif kind is ExtensionKind.MEMORY_PROVIDER:
            if operation == "remember":
                result = await item.remember(
                    MemoryRecord(
                        content=str(data.pop("content", "")),
                        kind=str(data.pop("kind", "fact")),
                        scope=dict(data.pop("scope", {})),
                        metadata=data,
                    )
                )
            elif operation == "forget":
                result = await item.forget(
                    str(data.pop("memory_id", "")),
                    scope=dict(data.pop("scope", {})),
                )
            else:
                result = await item.recall(
                    MemoryQuery(
                        text=str(data.pop("text", data.pop("query", ""))),
                        scopes=tuple(data.pop("scopes", ())),
                        limit=int(data.pop("limit", 10)),
                        metadata=data,
                    )
                )
        elif kind is ExtensionKind.CHANNEL_CONNECTOR:
            if hasattr(item, "handle") and operation not in {"send", "receive"}:
                result = await item.handle(
                    ExtensionRequest(operation=operation, payload=data, session_id=session_id)
                )
            elif operation == "receive":
                return await item.receive(limit=int(data.get("limit", 1)))
            else:
                result = await item.send(
                    ChannelMessage(
                        channel=str(data.pop("channel", extension_id)),
                        conversation_id=str(
                            data.pop("conversation_id", data.pop("chat_id", ""))
                        ),
                        text=str(data.pop("text", "")),
                        parts=tuple(data.pop("parts", ())),
                        metadata=data,
                    )
                )
        elif kind is ExtensionKind.RUNTIME_SERVICE:
            result = await item.handle(
                ExtensionRequest(operation=operation, payload=data, session_id=session_id)
            )
        elif kind is ExtensionKind.STORAGE_PROVIDER:
            key = str(data.pop("key", ""))
            namespace = str(data.pop("namespace", ""))
            if operation == "read":
                return await item.read(key, namespace=namespace)
            if operation == "write":
                await item.write(
                    key,
                    data.pop("value", None),
                    namespace=namespace,
                )
                return None
            if operation == "delete":
                await item.delete(key, namespace=namespace)
                return None
            raise ValueError("storage operation must be read, write or delete")
        elif kind is ExtensionKind.OBSERVABILITY:
            raise TypeError(
                "observability providers receive records from the host and "
                "cannot be invoked as commands"
            )
        elif hasattr(item, "invoke"):
            result = await item.invoke(
                ExtensionRequest(operation=operation, payload=data, session_id=session_id)
            )
        else:
            raise TypeError(f"unsupported extension kind: {kind.value}")
        if getattr(result, "is_success", False):
            return result.payload
        error = getattr(result, "error", None)
        raise RuntimeError(
            getattr(error, "message", None)
            or f"extension {extension_id!r} failed"
        )

    async def stream_extension(
        self,
        extension_id: str,
        payload: dict | None = None,
        *,
        session_id: str = "",
    ):
        """Stream from a model provider without admitting it to the tool kernel."""
        item = self.models.get(extension_id)
        if not hasattr(item, "stream_generate_events"):
            raise TypeError(f"model provider {extension_id!r} does not support streaming")
        data = dict(payload or {})
        data.pop("operation", None)
        prompt = str(data.pop("prompt", ""))
        messages = await self.augment_with_skills(
            tuple(data.pop("messages", ())),
            prompt=prompt,
            session_id=session_id,
        )
        messages = await self.augment_with_hooks(
            messages,
            prompt=prompt,
            session_id=session_id,
        )
        messages = self._with_temporal_context(messages)
        model_key = str(data.get("model") or extension_id)
        request, context, prompt_bytes = await self._prepare_model_request(
            item,
            prompt=prompt,
            messages=messages,
            data=data,
            session_id=session_id,
            model_key=model_key,
        )
        correlation_id = getattr(request, "request_id", "") or f"stream-{session_id}"
        await self.record_observation(
            TraceStage.PLANNER_CALLED,
            correlation_id=correlation_id,
            session_id=session_id,
            capability_id=extension_id,
            metadata={
                "model": request.model or "",
                "modalities": list(request.modalities),
                "operation": "stream",
            },
        )
        try:
            if context is not None and request.messages:
                yield context
            async for event in item.stream_generate_events(request):
                if (
                    event.get("type") == "context"
                    and event.get("source") == "provider"
                    and event.get("scope") == "prompt"
                ):
                    self._record_context_calibration(
                        model_key,
                        prompt_bytes=prompt_bytes,
                        prompt_tokens=event.get("used"),
                    )
                yield event
        except Exception as exc:
            await self.record_observation(
                TraceStage.ERROR,
                correlation_id=correlation_id,
                session_id=session_id,
                capability_id=extension_id,
                metadata={"error_type": type(exc).__name__, "operation": "stream"},
            )
            raise
        else:
            await self.record_observation(
                TraceStage.RESULT_PUBLISHED,
                correlation_id=correlation_id,
                session_id=session_id,
                capability_id=extension_id,
                metadata={"operation": "stream"},
            )
        finally:
            await self._dispatch_hooks(
                "post_model_call",
                {
                    "session_id": session_id,
                    "model": request.model or "",
                    "streamed": True,
                },
            )

    def _populate_extensions(self) -> None:
        self.extensions.clear()
        for kind_name, extension_ids in self.config.extensions.active.items():
            try:
                expected_kind = ExtensionKind(kind_name)
            except ValueError:
                self.log.warning("unknown extension kind '%s' — skipping", kind_name)
                continue
            for extension_id in extension_ids:
                spec = self.config.extensions.available.get(extension_id)
                if spec is None or not spec.enabled:
                    continue
                item = self._build_extension(extension_id, spec)
                if item is None:
                    continue
                if item.kind is not expected_kind:
                    self.log.warning(
                        "extension '%s' loaded as %s, configured as %s",
                        extension_id,
                        item.kind.value,
                        expected_kind.value,
                    )
                    continue
                self.extensions.register(extension_id, item)

    def _build_extension(self, extension_id: str, spec: ExtensionSpec) -> Any | None:
        factory = _BUILTIN_FACTORIES.get(extension_id)
        if factory is not None:
            return factory()
        return self.extension_loader.load(extension_id, spec)

    def _apply_environment(self) -> None:
        self._apply_llm_environment()
        self._apply_telegram_environment()
        self._apply_websearch_environment()
        self._apply_gateway_environment()
        self._apply_state_environment()
        self._apply_security_environment()
        self._apply_skills_environment()
        self._apply_sandbox_environment()
        self._apply_observability_environment()

    def _apply_llm_environment(self) -> None:
        llm = self.config.llm
        os.environ["CORAX_LLM_BASE_URL"] = llm.base_url
        os.environ["CORAX_LLM_MODEL"] = llm.model
        os.environ["CORAX_LLM_ENABLE_IMAGE"] = "true" if llm.enable_image else "false"
        os.environ["CORAX_LLM_ENABLE_VIDEO"] = "true" if llm.enable_video else "false"

    def _apply_telegram_environment(self) -> None:
        telegram = self.config.telegram
        os.environ["CORAX_TELEGRAM_BASE_URL"] = telegram.base_url
        os.environ["CORAX_TELEGRAM_ALLOWED_CHATS"] = telegram.allowed_chats

    def _apply_websearch_environment(self) -> None:
        websearch = self.config.websearch
        os.environ["CORAX_WEBSEARCH_BASE_URL"] = websearch.base_url
        for env_name, value in (
            ("CORAX_WEBSEARCH_ENGINES", websearch.engines),
            ("CORAX_WEBSEARCH_LANGUAGE", websearch.language),
            ("CORAX_WEBSEARCH_SAFESEARCH", websearch.safesearch),
        ):
            if value:
                os.environ[env_name] = value
            else:
                os.environ.pop(env_name, None)

    def _apply_gateway_environment(self) -> None:
        self._set_default_environment(
            "CORAX_GATEWAY_STATE_PATH",
            str(self.data_path / "gateway-state.json"),
        )

    def _apply_state_environment(self) -> None:
        self._set_default_environment(
            "CORAX_STATE_PATH",
            str(self.data_path / "state"),
        )

    def _apply_security_environment(self) -> None:
        self._set_default_environment("CORAX_SECURITY_MODE", self.config.security.mode)
        self._set_default_environment(
            "CORAX_SECURITY_STATE_PATH",
            str(self.data_path / "security-policy.json"),
        )
        self._set_default_environment(
            "CORAX_SECURITY_AUDIT_PATH",
            str(self.data_path / "security-policy.audit.jsonl"),
        )
        blocked_paths = ",".join(
            path.strip()
            for path in self.config.security.blocked_paths
            if isinstance(path, str) and path.strip()
        )
        if blocked_paths:
            os.environ["CORAX_SECURITY_BLOCKED_PATHS"] = blocked_paths
        else:
            os.environ.pop("CORAX_SECURITY_BLOCKED_PATHS", None)

    def _apply_skills_environment(self) -> None:
        self._set_default_environment(
            "CORAX_SKILLS_PATHS",
            os.pathsep.join(
                (
                    str(self.root_path / "skills"),
                    str(self.root_path / ".agents" / "skills"),
                )
            ),
        )

    def _apply_sandbox_environment(self) -> None:
        os.environ["CORAX_SHELL_REQUIRE_SANDBOX"] = "true"
        os.environ["CORAX_SANDBOX_WORKSPACE"] = str(self.workspace_path)

    def _apply_observability_environment(self) -> None:
        self._set_default_environment(
            "CORAX_OBSERVABILITY_PATH",
            str(self.data_path / "observability" / "traces.jsonl"),
        )

    def _set_default_environment(self, name: str, value: str) -> None:
        """Update runtime-owned defaults without replacing operator overrides."""

        previous = self._managed_environment.get(name)
        current = os.environ.get(name)
        if current is None or current == previous:
            os.environ[name] = value
            self._managed_environment[name] = value
        else:
            self._managed_environment.pop(name, None)
