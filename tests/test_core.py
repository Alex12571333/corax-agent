"""agent-core seam: CoreEngine introspection + end-to-end execution.

These tests need ``agent-core`` importable. They are self-contained: a tiny
SAFE capability is defined here, so they do not depend on the SDK capability
packages or their policy levels.
"""

from __future__ import annotations

import asyncio
import unittest

try:
    import agent_core  # noqa: F401

    HAS_AGENT_CORE = True
except ImportError:  # pragma: no cover - exercised on stdlib-only installs
    HAS_AGENT_CORE = False

from corax import config as cfg
from corax.capabilities import EchoCapability
from corax.loader import ConfirmationRequired, CoreEngine
from corax.runtime import CoraxRuntime


def _make_adder():
    """A minimal, well-formed SAFE capability that the default policy allows."""
    from agent_core import (
        Capability,
        CapabilityRequest,
        HealthStatus,
        PermissionLevel,
        Result,
        RiskLevel,
    )

    class _Adder(Capability):
        id = "adder"
        name = "Adder"
        description = "Adds two integers."
        version = "1.0.0"
        tags = {"math"}
        permission_level = PermissionLevel.SAFE
        required_scopes: set[str] = set()
        risk_level = RiskLevel.LOW
        side_effects: set = set()
        input_schema: dict = {}
        output_schema: dict = {}

        def __init__(self) -> None:
            self.calls: list[int] = []

        async def execute(self, request: CapabilityRequest) -> Result:
            total = int(request.input.get("a", 0)) + int(request.input.get("b", 0))
            self.calls.append(total)
            return Result.ok(
                {"sum": total},
                session_id=request.session_id,
                task_id=request.task_id,
            )

        async def health_check(self) -> HealthStatus:
            return HealthStatus.HEALTHY

    return _Adder()


def _make_confirmed_writer():
    from agent_core import (
        Capability,
        CapabilityRequest,
        HealthStatus,
        PermissionLevel,
        Result,
        RiskLevel,
        SideEffect,
    )

    class _Writer(Capability):
        id = "writer"
        name = "Writer"
        description = "A test action requiring confirmation."
        version = "1.0.0"
        tags = {"write"}
        permission_level = PermissionLevel.CONFIRM
        required_scopes = {"file.write"}
        risk_level = RiskLevel.MEDIUM
        side_effects = {SideEffect.WRITE_FILE}
        input_schema: dict = {}
        output_schema: dict = {}

        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, request: CapabilityRequest) -> Result:
            self.calls += 1
            return Result.ok(
                {"written": True},
                session_id=request.session_id,
                task_id=request.task_id,
            )

        async def health_check(self) -> HealthStatus:
            return HealthStatus.HEALTHY

    return _Writer()


@unittest.skipUnless(HAS_AGENT_CORE, "agent-core not installed")
class TestCoreEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = CoreEngine(cfg.default_config())

    def test_available(self) -> None:
        self.assertTrue(self.engine.available)

    def test_executable_ids_filters_non_core(self) -> None:
        pairs = [("adder", _make_adder()), ("echo", EchoCapability())]
        # Both are real tool contracts; runtime infrastructure is kept in
        # separate registries before this boundary.
        self.assertEqual(self.engine.executable_ids(pairs), ["adder", "echo"])

    def test_executes_task_through_kernel(self) -> None:
        from agent_core import TaskStatus

        adder = _make_adder()

        async def go():
            async with self.engine.session([("adder", adder)]) as kernel:
                self.assertEqual(kernel.capability_ids, ["adder"])
                return await kernel.run_task(
                    required_capability="adder", input={"a": 2, "b": 3}
                )

        task = asyncio.run(go())
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(adder.calls, [5])

    def test_session_forwards_core_traces_to_observability_provider(self) -> None:
        adder = _make_adder()

        class _Sink:
            def __init__(self) -> None:
                self.records = []

            async def record(self, record) -> None:
                self.records.append(record)

        sink = _Sink()

        async def go() -> None:
            async with self.engine.session(
                [("adder", adder)],
                observability=sink,
            ) as kernel:
                await kernel.run_task(
                    required_capability="adder",
                    input={"a": 1, "b": 1},
                )

        asyncio.run(go())
        stages = {
            getattr(record.stage, "value", str(record.stage))
            for record in sink.records
        }
        self.assertIn("capability_called", stages)
        self.assertIn("capability_completed", stages)

    def test_invoke_parks_and_resumes_after_confirmation(self) -> None:
        writer = _make_confirmed_writer()

        async def go():
            async with self.engine.session([("writer", writer)]) as kernel:
                with self.assertRaises(ConfirmationRequired) as raised:
                    await kernel.invoke("writer", {"path": "notes.txt"})
                return await kernel.resolve_confirmation(
                    raised.exception.task_id,
                    approved=True,
                    actor="test",
                )

        result = asyncio.run(go())
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], {"written": True})
        self.assertEqual(writer.calls, 1)

    def test_session_unavailable_raises_when_core_absent(self) -> None:
        engine = CoreEngine(cfg.default_config())
        engine._probed = True
        engine._ac = None  # force the "not installed" branch

        async def go():
            async with engine.session():
                pass

        with self.assertRaises(RuntimeError):
            asyncio.run(go())


@unittest.skipUnless(HAS_AGENT_CORE, "agent-core not installed")
class TestRuntimeCore(unittest.TestCase):
    def test_runtime_execute_runs_capability_via_core(self) -> None:
        from agent_core import TaskStatus

        runtime = CoraxRuntime(cfg.default_config())
        adder = _make_adder()

        async def go():
            await runtime.start()
            runtime.capabilities.register("adder", adder)
            task = await runtime.execute("adder", input={"a": 4, "b": 6})
            await runtime.stop()
            return task

        task = asyncio.run(go())
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(adder.calls, [10])

    def test_snapshot_reports_core(self) -> None:
        runtime = CoraxRuntime(cfg.default_config())

        async def go():
            await runtime.start()
            snap = runtime.snapshot()
            await runtime.stop()
            return snap

        snap = asyncio.run(go())
        self.assertTrue(snap.core_available)
        self.assertIn("core_available", snap.to_dict())
        self.assertIn("core (tools)", snap.render())


if __name__ == "__main__":
    unittest.main()
