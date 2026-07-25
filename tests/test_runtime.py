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
    "gateway": REPO_ROOT.parent / "corax-gateway-capability",
    "security.policy": REPO_ROOT.parent / "corax-security-policy",
    "memory.loop": REPO_ROOT.parent / "corax-memory-loop",
    "console.connector": REPO_ROOT.parent / "corax-console",
    "state.file": REPO_ROOT.parent / "corax-state-store",
    "context.manager": REPO_ROOT.parent / "corax-context-manager",
    "mcp.manager": REPO_ROOT.parent / "corax-mcp-manager",
    "skills.runtime": REPO_ROOT.parent / "corax-skills-runtime",
    "hooks.runtime": REPO_ROOT.parent / "corax-hooks-runtime",
    "subagents.orchestrator": REPO_ROOT.parent / "corax-subagents",
    "sandbox.executor": REPO_ROOT.parent / "corax-sandbox-executor",
    "model.router": REPO_ROOT.parent / "corax-model-router",
    "observability.jsonl": REPO_ROOT.parent / "corax-observability",
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
        self.assertIsInstance(self.runtime.memory.get("memory.none"), NullMemory)
        if HAS_AGENT_SDK and CAPABILITY_ROOTS["console.connector"].is_dir():
            self.assertTrue(self.runtime.connectors.has("console.connector"))
        self.assertIsInstance(self.runtime.capabilities.get("echo"), EchoCapability)
        # The package capabilities load only when agent-sdk is installed AND the
        # sibling repos are present on disk.
        if HAS_AGENT_SDK:
            for cap_id in ("filesystem", "editor", "shell"):
                if CAPABILITY_ROOTS[cap_id].is_dir():
                    self.assertTrue(self.runtime.capabilities.has(cap_id))
            if CAPABILITY_ROOTS["gateway"].is_dir():
                self.assertTrue(self.runtime.services.has("gateway"))
            if CAPABILITY_ROOTS["security.policy"].is_dir():
                self.assertTrue(self.runtime.policies.has("security.policy"))
            if CAPABILITY_ROOTS["memory.loop"].is_dir():
                self.assertTrue(self.runtime.services.has("memory.loop"))
                self.assertIsNotNone(self.runtime.active_memory_loop())
            if CAPABILITY_ROOTS["state.file"].is_dir():
                self.assertTrue(self.runtime.storage.has("state.file"))
                self.assertIsNotNone(self.runtime.active_state_store())
            if CAPABILITY_ROOTS["context.manager"].is_dir():
                self.assertTrue(self.runtime.services.has("context.manager"))
                self.assertIsNotNone(self.runtime.active_context_manager())
            if CAPABILITY_ROOTS["mcp.manager"].is_dir():
                self.assertTrue(self.runtime.services.has("mcp.manager"))
                self.assertIsNotNone(self.runtime.active_mcp_manager())
            if CAPABILITY_ROOTS["skills.runtime"].is_dir():
                self.assertTrue(self.runtime.services.has("skills.runtime"))
                self.assertIsNotNone(self.runtime.active_skills_runtime())
            if CAPABILITY_ROOTS["hooks.runtime"].is_dir():
                self.assertTrue(self.runtime.services.has("hooks.runtime"))
                self.assertIsNotNone(self.runtime.active_hooks_runtime())
            if CAPABILITY_ROOTS["subagents.orchestrator"].is_dir():
                self.assertTrue(self.runtime.services.has("subagents.orchestrator"))
                self.assertIsNotNone(self.runtime.active_subagent_orchestrator())
                self.assertTrue(self.runtime.tools.has("subagents.delegate"))
            if CAPABILITY_ROOTS["sandbox.executor"].is_dir():
                self.assertTrue(self.runtime.services.has("sandbox.executor"))
                self.assertIsNotNone(self.runtime.active_sandbox_executor())
            if CAPABILITY_ROOTS["model.router"].is_dir():
                self.assertTrue(self.runtime.models.has("model.router"))
                self.assertIsNotNone(self.runtime.active_model_router())
            if CAPABILITY_ROOTS["observability.jsonl"].is_dir():
                self.assertTrue(
                    self.runtime.observability.has("observability.jsonl")
                )
                self.assertIsNotNone(self.runtime.active_observability())
        self.assertFalse(self.runtime.capabilities.has("llm.local"))
        self.assertFalse(self.runtime.capabilities.has("telegram.connector"))
        self.assertFalse(self.runtime.capabilities.has("gateway"))

    def test_status_after_start(self) -> None:
        asyncio.run(self.runtime.start())
        status = asyncio.run(self.runtime.status())
        self.assertTrue(status.running)
        self.assertEqual(status.planner_active, "stub")
        self.assertEqual(status.memory_active, "memory.none")
        self.assertEqual(
            status.connectors_active,
            ["console.connector", "telegram.connector"],
        )
        self.assertEqual(
            status.capabilities_enabled,
            [
                "echo",
                "filesystem",
                "editor",
                "shell",
                "web.search",
                "subagents.delegate",
            ],
        )
        self.assertEqual(status.registry_counts["model_provider"], 3)
        self.assertEqual(
            status.active_by_kind["runtime_service"],
            [
                "gateway",
                "memory.loop",
                "context.manager",
                "mcp.manager",
                "skills.runtime",
                "hooks.runtime",
                "subagents.orchestrator",
                "sandbox.executor",
            ],
        )
        self.assertEqual(status.active_by_kind["storage_provider"], ["state.file"])
        self.assertEqual(
            status.active_by_kind["observability"],
            ["observability.jsonl"],
        )
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
        new_config.extensions.active["channel_connector"] = []
        new_config.refresh_legacy_views()
        asyncio.run(self.runtime.reload_config(new_config))
        self.assertTrue(self.runtime.running)
        self.assertEqual(len(self.runtime.connectors), 0)

    def test_unknown_provider_is_skipped(self) -> None:
        self.config.extensions.active["model_provider"] = ["openai"]
        self.config.extensions.bindings["planner"] = "openai"
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

    def test_start_exports_websearch_environment(self) -> None:
        import os

        # A stale safesearch must be cleared, not exported as an empty string.
        os.environ["CORAX_WEBSEARCH_SAFESEARCH"] = "9"
        config = cfg.default_config()
        config.websearch.base_url = "http://192.168.0.50:8888"
        config.websearch.engines = "duckduckgo,brave"
        config.websearch.language = "en"
        config.websearch.safesearch = ""  # unset -> not exported
        runtime = CoraxRuntime(config)
        asyncio.run(runtime.start())
        try:
            self.assertEqual(os.environ["CORAX_WEBSEARCH_BASE_URL"], "http://192.168.0.50:8888")
            self.assertEqual(os.environ["CORAX_WEBSEARCH_ENGINES"], "duckduckgo,brave")
            self.assertEqual(os.environ["CORAX_WEBSEARCH_LANGUAGE"], "en")
            self.assertNotIn("CORAX_WEBSEARCH_SAFESEARCH", os.environ)
        finally:
            asyncio.run(runtime.stop())
            os.environ.pop("CORAX_WEBSEARCH_SAFESEARCH", None)

    def test_start_exports_gateway_state_path(self) -> None:
        import os

        old_value = os.environ.pop("CORAX_GATEWAY_STATE_PATH", None)
        with tempfile.TemporaryDirectory() as tmpdir:
            config = cfg.default_config()
            config.runtime.data_path = "custom-data"
            runtime = CoraxRuntime(config, root_path=tmpdir)
            asyncio.run(runtime.start())
            try:
                self.assertEqual(
                    os.environ["CORAX_GATEWAY_STATE_PATH"],
                    str((Path(tmpdir) / "custom-data" / "gateway-state.json").resolve()),
                )
            finally:
                asyncio.run(runtime.stop())
                if old_value is None:
                    os.environ.pop("CORAX_GATEWAY_STATE_PATH", None)
                else:
                    os.environ["CORAX_GATEWAY_STATE_PATH"] = old_value

    def test_memory_loop_is_wired_to_selected_provider(self) -> None:
        asyncio.run(self.runtime.start())
        result = asyncio.run(
            self.runtime.memory_before_turn(
                "what do you remember?",
                session_id="memory-session",
            )
        )
        self.assertEqual(result["context"], "")
        self.assertEqual(result["provider"], "memory.none")

    def test_context_manager_compacts_old_history(self) -> None:
        asyncio.run(self.runtime.start())
        messages = [{"role": "system", "content": "safety"}]
        for index in range(20):
            messages.append(
                {"role": "assistant", "content": f"old-{index}-" * 1_000}
            )
        messages.append({"role": "user", "content": "current"})
        compacted = asyncio.run(
            self.runtime.compact_messages(messages, session_id="context-test")
        )
        self.assertLess(len(compacted), len(messages))
        self.assertEqual(compacted[0]["content"], "safety")
        self.assertEqual(compacted[-1]["content"], "current")

    def test_stream_reports_exact_host_context_budget(self) -> None:
        from agent_core import ExtensionKind

        class ContextManager:
            id = "context.manager"
            kind = ExtensionKind.RUNTIME_SERVICE
            max_chars = 48_000

            async def handle(self, request):
                return type(
                    "Result",
                    (),
                    {
                        "is_success": True,
                        "payload": {
                            "messages": list(request.payload["messages"]),
                            "chars_after": 137,
                        },
                    },
                )()

        class StreamingModel:
            id = "stream.test"
            kind = ExtensionKind.MODEL_PROVIDER

            async def stream_generate_events(self, request):
                yield {"type": "delta", "content": "ok"}
                yield {"type": "done"}

        self.runtime.services.register("context.manager", ContextManager())
        self.runtime.models.register("stream.test", StreamingModel())

        async def collect() -> list[dict]:
            return [
                event
                async for event in self.runtime.stream_extension(
                    "stream.test",
                    {"messages": [{"role": "user", "content": "hello"}]},
                    session_id="context-stream",
                )
            ]

        events = asyncio.run(collect())
        self.assertEqual(
            events,
            [
                {
                    "type": "context",
                    "used": 137,
                    "limit": 48_000,
                    "unit": "chars",
                },
                {"type": "delta", "content": "ok"},
                {"type": "done"},
            ],
        )

    def test_skills_runtime_augments_only_matching_turn(self) -> None:
        import os
        import tempfile

        old_value = os.environ.get("CORAX_SKILLS_PATHS")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "code-review"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: code-review\n"
                "description: Review source code changes and diffs.\n"
                "---\n\n"
                "Inspect the complete diff before reviewing.\n",
                encoding="utf-8",
            )
            os.environ["CORAX_SKILLS_PATHS"] = str(root)
            runtime = CoraxRuntime(cfg.default_config())
            asyncio.run(runtime.start())
            try:
                messages = asyncio.run(
                    runtime.augment_with_skills(
                        [{"role": "user", "content": "Review this code diff"}],
                        session_id="skills-test",
                    )
                )
                self.assertEqual(messages[-1]["role"], "user")
                self.assertIn("complete diff", messages[-2]["content"])
            finally:
                asyncio.run(runtime.stop())
                if old_value is None:
                    os.environ.pop("CORAX_SKILLS_PATHS", None)
                else:
                    os.environ["CORAX_SKILLS_PATHS"] = old_value

    def test_start_keeps_custom_gateway_state_path(self) -> None:
        import os

        old_value = os.environ.get("CORAX_GATEWAY_STATE_PATH")
        os.environ["CORAX_GATEWAY_STATE_PATH"] = "/tmp/custom-corax-gateway.json"
        runtime = CoraxRuntime(cfg.default_config())
        asyncio.run(runtime.start())
        try:
            self.assertEqual(os.environ["CORAX_GATEWAY_STATE_PATH"], "/tmp/custom-corax-gateway.json")
        finally:
            asyncio.run(runtime.stop())
            if old_value is None:
                os.environ.pop("CORAX_GATEWAY_STATE_PATH", None)
            else:
                os.environ["CORAX_GATEWAY_STATE_PATH"] = old_value


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
            config.extensions.available[cap_id].path = str(path)
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
                        "operation": "validate",
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
        self.assertTrue(shell_result.payload["safe"])
        self.assertIs(
            getattr(shell, "_executor", None),
            self.runtime.active_sandbox_executor(),
        )

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
