"""Runtime: starts with built-ins, reports status, reloads, populates registries."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

try:  # agent-core is only needed for the live capability integration test.
    from agent_core import CapabilityRequest, ResultStatus

    HAS_AGENT_CORE = True
except ImportError:  # pragma: no cover - exercised on stdlib-only installs
    HAS_AGENT_CORE = False

try:  # agent-sdk is what actually loads the filesystem/editor/shell packages.
    import agent_sdk  # noqa: F401

    HAS_AGENT_SDK = True
except ImportError:  # pragma: no cover
    HAS_AGENT_SDK = False

from corax import config as cfg
from corax.capabilities import EchoCapability
from corax.connectors import TerminalConnector
from corax.memory import NullMemory
from corax.planner import StubPlanner
from corax.runtime import CoraxRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]
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

    def test_start_populates_registries_with_builtins(self) -> None:
        asyncio.run(self.runtime.start())
        self.assertTrue(self.runtime.running)
        self.assertIsInstance(self.runtime.providers.get("stub"), StubPlanner)
        self.assertIsInstance(self.runtime.memory.get("none"), NullMemory)
        self.assertIsInstance(self.runtime.connectors.get("terminal"), TerminalConnector)
        self.assertIsInstance(self.runtime.capabilities.get("echo"), EchoCapability)
        # The package capabilities load only when agent-sdk is installed AND the
        # sibling repos are present on disk.
        if HAS_AGENT_SDK:
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
            ["echo", "filesystem", "editor", "shell", "llm.local", "telegram.connector"],
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
        self.config.planner.active = "openai"  # no built-in for it
        asyncio.run(self.runtime.start())
        self.assertFalse(self.runtime.providers.has("openai"))
        self.assertEqual(len(self.runtime.providers), 0)

    def test_start_exports_llm_environment(self) -> None:
        import os

        config = cfg.default_config()
        config.llm.base_url = "http://192.168.0.10:9999/v1"
        config.llm.model = "google/gemma-4-12B-it"
        config.llm.enable_image = True
        config.llm.enable_video = False
        runtime = CoraxRuntime(config)
        asyncio.run(runtime.start())
        try:
            self.assertEqual(os.environ["CORAX_LLM_BASE_URL"], "http://192.168.0.10:9999/v1")
            self.assertEqual(os.environ["CORAX_LLM_MODEL"], "google/gemma-4-12B-it")
            self.assertEqual(os.environ["CORAX_LLM_ENABLE_IMAGE"], "true")
            self.assertEqual(os.environ["CORAX_LLM_ENABLE_VIDEO"], "false")
        finally:
            asyncio.run(runtime.stop())

    def test_start_exports_telegram_environment(self) -> None:
        import os

        config = cfg.default_config()
        config.telegram.base_url = "https://tg.example/api"
        config.telegram.allowed_chats = "100,200"
        runtime = CoraxRuntime(config)
        asyncio.run(runtime.start())
        try:
            self.assertEqual(os.environ["CORAX_TELEGRAM_BASE_URL"], "https://tg.example/api")
            self.assertEqual(os.environ["CORAX_TELEGRAM_ALLOWED_CHATS"], "100,200")
        finally:
            asyncio.run(runtime.stop())


class TestCapabilityIntegration(unittest.TestCase):
    def setUp(self) -> None:
        if not HAS_AGENT_CORE:
            self.skipTest("agent-core / agent-sdk not installed")
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

    def _request(self, payload: dict) -> "CapabilityRequest":
        return CapabilityRequest(
            task_id="task-1",
            session_id="session-1",
            input=payload,
        )


class TestBuiltins(unittest.TestCase):
    def test_echo_capability_returns_input(self) -> None:
        cap = EchoCapability()
        self.assertEqual(asyncio.run(cap.invoke({"text": "hi"})), {"text": "hi"})

    def test_planner_produces_echo_task(self) -> None:
        plan = asyncio.run(StubPlanner().plan("do a thing"))
        self.assertEqual(plan["goal"], "do a thing")
        self.assertEqual(plan["tasks"][0]["capability"], "echo")

    def test_memory_is_empty(self) -> None:
        mem = NullMemory()
        self.assertEqual(asyncio.run(mem.query("anything")), [])
        self.assertFalse(asyncio.run(mem.store("k", "v")))


if __name__ == "__main__":
    unittest.main()
