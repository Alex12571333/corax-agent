"""Runtime: starts with stubs, reports status, reloads, populates registries."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from agent_core import CapabilityRequest, ResultStatus

from corax_agent import config as cfg
from corax_agent.runtime import CoraxRuntime
from corax_agent.stubs import CapabilityStub, ConnectorStub, MemoryStub, PlannerStub

REPO_ROOT = Path(__file__).resolve().parents[2]
CAPABILITY_ROOTS = {
    "filesystem": REPO_ROOT.parent / "corax-filesystem-capability",
    "editor": REPO_ROOT.parent / "corax-editor-capability",
    "shell": REPO_ROOT.parent / "corax-shell-capability",
}


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
        for cap_id in ("filesystem", "editor", "shell"):
            if CAPABILITY_ROOTS[cap_id].is_dir():
                self.assertTrue(self.runtime.capabilities.has(cap_id))

    def test_status_after_start(self) -> None:
        asyncio.run(self.runtime.start())
        status = asyncio.run(self.runtime.status())
        self.assertTrue(status.running)
        self.assertEqual(status.planner_active, "stub")
        self.assertEqual(status.memory_active, "none")
        self.assertEqual(status.connectors_active, ["terminal"])
        self.assertEqual(
            status.capabilities_enabled,
            ["stub.echo", "filesystem", "editor", "shell"],
        )
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


class TestCapabilityIntegration(unittest.TestCase):
    def setUp(self) -> None:
        missing = [str(path) for path in CAPABILITY_ROOTS.values() if not path.is_dir()]
        if missing:
            self.skipTest(f"local capability repositories are missing: {missing}")
        self.tempdir = tempfile.TemporaryDirectory()
        workspace = Path(self.tempdir.name)
        config = cfg.default_config()
        for cap_id, path in CAPABILITY_ROOTS.items():
            config.capabilities.available[cap_id].path = str(path)
        self.runtime = CoraxRuntime(
            config,
            root_path=REPO_ROOT,
            workspace_path=workspace,
        )

    def tearDown(self) -> None:
        asyncio.run(self.runtime.stop())
        self.tempdir.cleanup()

    def test_filesystem_editor_shell_work_as_one_runtime(self) -> None:
        asyncio.run(self.runtime.start())

        filesystem = self.runtime.capabilities.get("filesystem")
        editor = self.runtime.capabilities.get("editor")
        shell = self.runtime.capabilities.get("shell")

        write = asyncio.run(
            filesystem.execute(
                self._request(
                    {
                        "operation": "write",
                        "path": "notes.txt",
                        "content": "hello\n",
                    }
                )
            )
        )
        edit = asyncio.run(
            editor.execute(
                self._request(
                    {
                        "operation": "replace",
                        "path": "notes.txt",
                        "old": "hello",
                        "new": "hello corax",
                    }
                )
            )
        )
        read = asyncio.run(
            filesystem.execute(
                self._request({"operation": "read", "path": "notes.txt"})
            )
        )
        shell_result = asyncio.run(
            shell.execute(
                self._request(
                    {
                        "command": "printf 'shell-ok\\n'",
                        "timeout_seconds": 5,
                    }
                )
            )
        )

        self.assertEqual(write.status, ResultStatus.SUCCESS)
        self.assertEqual(edit.status, ResultStatus.SUCCESS)
        self.assertEqual(read.status, ResultStatus.SUCCESS)
        self.assertEqual(read.payload["content"], "hello corax\n")
        self.assertEqual(shell_result.status, ResultStatus.SUCCESS)
        self.assertEqual(shell_result.payload["stdout"], "shell-ok\n")

    def _request(self, payload: dict) -> CapabilityRequest:
        return CapabilityRequest(
            task_id="task-1",
            session_id="session-1",
            input=payload,
        )


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
