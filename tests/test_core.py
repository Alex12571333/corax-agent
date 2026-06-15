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
from corax.loader import CoreEngine
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


@unittest.skipUnless(HAS_AGENT_CORE, "agent-core not installed")
class TestCoreEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = CoreEngine(cfg.default_config())

    def test_available(self) -> None:
        self.assertTrue(self.engine.available)

    def test_executable_ids_filters_non_core(self) -> None:
        pairs = [("adder", _make_adder()), ("echo", EchoCapability())]
        # Only the real agent_core.Capability is executable; the built-in
        # echo placeholder is not.
        self.assertEqual(self.engine.executable_ids(pairs), ["adder"])

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
        self.assertIn("core (kernel)", snap.render())


if __name__ == "__main__":
    unittest.main()
