"""Runtime: starts with stubs, reports status, reloads, populates registries."""

from __future__ import annotations

import asyncio
import unittest

from corax_agent import config as cfg
from corax_agent.runtime import CoraxRuntime
from corax_agent.stubs import CapabilityStub, ConnectorStub, MemoryStub, PlannerStub


class TestRuntime(unittest.TestCase):
    def setUp(self) -> None:
        self.config = cfg.default_config()
        self.runtime = CoraxRuntime(self.config)

    def tearDown(self) -> None:
        asyncio.run(self.runtime.stop())

    def test_start_populates_registries_with_stubs(self) -> None:
        asyncio.run(self.runtime.start())
        self.assertTrue(self.runtime.running)
        self.assertIsInstance(self.runtime.providers.get("stub"), PlannerStub)
        self.assertIsInstance(self.runtime.memory.get("none"), MemoryStub)
        self.assertIsInstance(self.runtime.connectors.get("terminal"), ConnectorStub)
        self.assertIsInstance(self.runtime.capabilities.get("stub.echo"), CapabilityStub)

    def test_status_after_start(self) -> None:
        asyncio.run(self.runtime.start())
        status = asyncio.run(self.runtime.status())
        self.assertTrue(status.running)
        self.assertEqual(status.planner_active, "stub")
        self.assertEqual(status.memory_active, "none")
        self.assertEqual(status.connectors_active, ["terminal"])
        self.assertEqual(status.capabilities_enabled, ["stub.echo"])
        self.assertEqual(status.registry_counts["providers"], 1)
        self.assertIn("RUNNING", status.render())
        self.assertIn("running", status.to_dict())

    def test_snapshot_before_start_is_stopped(self) -> None:
        snap = self.runtime.snapshot()
        self.assertFalse(snap.running)
        self.assertIsNone(snap.started_at)

    def test_stop_clears_registries(self) -> None:
        asyncio.run(self.runtime.start())
        asyncio.run(self.runtime.stop())
        self.assertFalse(self.runtime.running)
        self.assertEqual(len(self.runtime.providers), 0)
        self.assertEqual(len(self.runtime.capabilities), 0)

    def test_reload_config_keeps_running(self) -> None:
        asyncio.run(self.runtime.start())
        new_config = cfg.default_config()
        new_config.connectors.active = []  # drop the terminal connector
        asyncio.run(self.runtime.reload_config(new_config))
        self.assertTrue(self.runtime.running)
        self.assertEqual(len(self.runtime.connectors), 0)

    def test_unknown_provider_is_skipped(self) -> None:
        self.config.planner.active = "openai"  # no stub for it
        asyncio.run(self.runtime.start())
        self.assertFalse(self.runtime.providers.has("openai"))
        self.assertEqual(len(self.runtime.providers), 0)


class TestStubBehaviour(unittest.TestCase):
    def test_echo_capability_returns_input(self) -> None:
        cap = CapabilityStub()
        self.assertEqual(asyncio.run(cap.invoke({"text": "hi"})), {"text": "hi"})

    def test_planner_produces_echo_task(self) -> None:
        plan = asyncio.run(PlannerStub().plan("do a thing"))
        self.assertEqual(plan["goal"], "do a thing")
        self.assertEqual(plan["tasks"][0]["capability"], "stub.echo")

    def test_memory_is_empty(self) -> None:
        mem = MemoryStub()
        self.assertEqual(asyncio.run(mem.query("anything")), [])
        self.assertFalse(asyncio.run(mem.store("k", "v")))


if __name__ == "__main__":
    unittest.main()
