"""Runtime: starts with built-ins, reports status, reloads, populates registries."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
    "web.search": REPO_ROOT.parent / "corax-web-search-capability",
    "web.fetch": REPO_ROOT.parent / "corax-web-search-capability" / "web_fetch",
    "gateway": REPO_ROOT.parent / "corax-gateway-capability",
    "security.policy": REPO_ROOT.parent / "corax-security-policy",
    "prompts.runtime": REPO_ROOT.parent / "corax-prompt-runtime",
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

    def test_model_tool_prefix_is_fixed_and_selected_schemas_are_context(self) -> None:
        class OfflineEmbeddings:
            async def embed(self, _texts, *, input_type):
                raise OSError("offline")

        self.runtime.tools.register("echo", EchoCapability())
        self.runtime.tool_routing.router.client = OfflineEmbeddings()

        with mock.patch.object(
            self.runtime,
            "active_prompt_runtime",
            return_value=SimpleNamespace(enabled=True),
        ):
            prepared = asyncio.run(
                self.runtime.prepare_tool_model_request(
                    {
                        "messages": [{"role": "user", "content": "echo this"}],
                        "_corax_recent_files": ["report.txt"],
                    },
                    session_id="s1",
                    turn_id="t1",
                    channel="console",
                )
            )

        self.assertEqual(
            [item["function"]["name"] for item in prepared["tools"]],
            ["tool_search", "tool_call"],
        )
        self.assertEqual(
            [
                item["id"]
                for item in prepared["_corax_prompt_context"][
                    "tool_descriptors"
                ]
            ],
            ["echo"],
        )
        self.assertEqual(
            prepared["_corax_prompt_context"]["recent_files"],
            ["report.txt"],
        )
        self.assertNotIn("_corax_recent_files", prepared)

    def test_disabled_prompt_runtime_uses_selected_legacy_tool_schemas(self) -> None:
        class OfflineEmbeddings:
            async def embed(self, _texts, *, input_type):
                raise OSError("offline")

        self.runtime.config.prompts.enabled = False
        self.runtime.tools.register("echo", EchoCapability())
        self.runtime.sync_tool_catalog()
        self.runtime.tool_routing.router.client = OfflineEmbeddings()

        prepared = asyncio.run(
            self.runtime.prepare_tool_model_request(
                {"messages": [{"role": "user", "content": "echo this"}]},
                session_id="legacy",
                turn_id="legacy-1",
                channel="console",
            )
        )

        self.assertIn(
            "echo",
            [item["function"]["name"] for item in prepared["tools"]],
        )
        self.assertNotIn("_corax_prompt_context", prepared)

    def test_new_turn_extends_provider_prefix_without_changing_meta_tools(self) -> None:
        from agent_core import ExtensionKind

        class OfflineEmbeddings:
            async def embed(self, _texts, *, input_type):
                raise OSError("offline")

        class Model:
            id = "prefix.test"
            kind = ExtensionKind.MODEL_PROVIDER

            def __init__(self) -> None:
                self.requests = []

            async def generate(self, request):
                self.requests.append(request)
                return SimpleNamespace(
                    is_success=True,
                    payload={"text": "ok", "cached_tokens": 0},
                    status=None,
                )

        async def run() -> None:
            await self.runtime.start()
            now = [datetime(2026, 7, 27, 23, 59, 59, tzinfo=timezone.utc)]
            self.runtime._clock = lambda: now[0]
            observations = []

            async def record_observation(*_args, **kwargs):
                observations.append(dict(kwargs.get("metadata") or {}))

            self.runtime.record_observation = record_observation
            model = Model()
            self.runtime.models.register(model.id, model)
            self.runtime.tool_routing.router.client = OfflineEmbeddings()
            session_id = "cache-prefix"

            first_user = {"role": "user", "content": "Inspect report.txt"}
            first = await self.runtime.prepare_tool_model_request(
                {"messages": [first_user]},
                session_id=session_id,
                turn_id="turn-1",
                channel="console",
            )
            await self.runtime.invoke_extension(
                model.id,
                first,
                session_id=session_id,
            )
            await self.runtime.memory_after_turn(
                first_user["content"],
                "The report is ready.",
                session_id=session_id,
                scope={"channel": "console"},
            )
            self.runtime.tool_routing.end_turn(
                session_id=session_id,
                channel="console",
            )
            now[0] = datetime(2026, 7, 28, 0, 0, 1, tzinfo=timezone.utc)

            second_user = {"role": "user", "content": "Summarize it"}
            second = await self.runtime.prepare_tool_model_request(
                {
                    "messages": [
                        first_user,
                        {"role": "assistant", "content": "The report is ready."},
                        second_user,
                    ]
                },
                session_id=session_id,
                turn_id="turn-2",
                channel="console",
            )
            await self.runtime.invoke_extension(
                model.id,
                second,
                session_id=session_id,
            )

            first_request, second_request = model.requests
            self.assertEqual(
                list(second_request.messages[: len(first_request.messages)]),
                list(first_request.messages),
            )
            self.assertEqual(
                first_request.parameters["tools"],
                second_request.parameters["tools"],
            )
            appended = "\n".join(
                str(message.get("content") or "")
                for message in second_request.messages[len(first_request.messages) :]
            )
            self.assertIn("Local date: 2026-07-28", appended)
            self.assertNotIn("_corax_prompt_context", second_request.parameters)
            self.assertNotIn("input_schema", str(observations))
            self.assertNotIn("active_tool_descriptors", str(observations))

        asyncio.run(run())

    def test_compacted_provider_epoch_is_replayed_on_next_turn(self) -> None:
        from agent_core import ExtensionKind

        class OfflineEmbeddings:
            async def embed(self, _texts, *, input_type):
                raise OSError("offline")

        class CompactOnce:
            id = "context.compact-once"
            kind = ExtensionKind.RUNTIME_SERVICE

            def __init__(self) -> None:
                self.calls = 0

            async def handle(self, request):
                messages = list(request.payload["messages"])
                self.calls += 1
                if self.calls == 1:
                    messages = [
                        messages[0],
                        {
                            "role": "user",
                            "content": (
                                '<turn-envelope visibility="model-only" '
                                'persistence="ram" kind="compaction-notice" '
                                'trust="runtime-data">compacted</turn-envelope>'
                            ),
                        },
                        *messages[-2:],
                    ]
                return SimpleNamespace(
                    is_success=True,
                    payload={"messages": messages, "overflow": False},
                )

        class Model:
            id = "compaction.model"
            kind = ExtensionKind.MODEL_PROVIDER

            def __init__(self) -> None:
                self.requests = []

            async def generate(self, request):
                self.requests.append(request)
                return SimpleNamespace(
                    is_success=True,
                    payload={"text": "ok", "cached_tokens": 0},
                    status=None,
                )

        async def run() -> None:
            await self.runtime.start()
            manager = CompactOnce()
            model = Model()
            self.runtime.services.register(manager.id, manager)
            self.config.extensions.bindings["context"] = manager.id
            self.runtime.models.register(model.id, model)
            self.runtime.tool_routing.router.client = OfflineEmbeddings()
            session_id = "compaction-prefix"
            old_history = [
                {"role": "user", "content": "old-0"},
                {"role": "assistant", "content": "old-answer"},
            ]
            current = {"role": "user", "content": "current?"}

            first = await self.runtime.prepare_tool_model_request(
                {"messages": [*old_history, current]},
                session_id=session_id,
                turn_id="turn-1",
                channel="console",
            )
            await self.runtime.invoke_extension(
                model.id,
                first,
                session_id=session_id,
            )
            await self.runtime.memory_after_turn(
                current["content"],
                "done",
                session_id=session_id,
                scope={"channel": "console"},
            )
            self.runtime.tool_routing.end_turn(
                session_id=session_id,
                channel="console",
            )

            second = await self.runtime.prepare_tool_model_request(
                {
                    "messages": [
                        *old_history,
                        current,
                        {"role": "assistant", "content": "done"},
                        {"role": "user", "content": "next?"},
                    ]
                },
                session_id=session_id,
                turn_id="turn-2",
                channel="console",
            )
            await self.runtime.invoke_extension(
                model.id,
                second,
                session_id=session_id,
            )

            first_request, second_request = model.requests
            self.assertEqual(
                list(second_request.messages[: len(first_request.messages)]),
                list(first_request.messages),
            )
            self.assertNotIn(
                "old-0",
                "\n".join(
                    str(message.get("content") or "")
                    for message in second_request.messages
                ),
            )

        asyncio.run(run())

    def test_disabled_prompt_runtime_uses_legacy_assembly(self) -> None:
        skill_messages = [{"role": "user", "content": "legacy"}]
        with (
            mock.patch.object(
                self.runtime,
                "active_prompt_runtime",
                return_value=SimpleNamespace(enabled=False),
            ),
            mock.patch.object(
                self.runtime,
                "augment_with_skills",
                new=mock.AsyncMock(return_value=skill_messages),
            ) as skills,
            mock.patch.object(
                self.runtime,
                "augment_with_hooks",
                new=mock.AsyncMock(return_value=skill_messages),
            ) as hooks,
        ):
            messages, metadata = asyncio.run(
                self.runtime._assemble_model_messages(
                    [{"role": "user", "content": "hello"}],
                    prompt="hello",
                    data={"_corax_prompt_context": {"turn_id": "t1"}},
                    session_id="s1",
                )
            )

        self.assertEqual(metadata, {})
        self.assertEqual(messages[-1], skill_messages[-1])
        self.assertEqual(messages[0]["role"], "system")
        skills.assert_awaited_once()
        hooks.assert_awaited_once()

    def test_abort_turn_discards_all_host_turn_state(self) -> None:
        end_turn = mock.AsyncMock(return_value={"committed": False})
        prompt_service = SimpleNamespace(enabled=True, end_turn=end_turn)
        routing_turn = SimpleNamespace(turn_id="t1")
        key = ("console", "s1", "t1")
        self.runtime._prompt_turn_inputs[key] = {"value": True}
        self.runtime._prompt_turn_metadata[key] = {"value": True}
        self.runtime._prompt_provider_messages[key] = [
            {"role": "user", "content": "hello"}
        ]

        with (
            mock.patch.object(
                self.runtime,
                "active_prompt_runtime",
                return_value=prompt_service,
            ),
            mock.patch.object(
                self.runtime.tool_routing,
                "current_turn",
                return_value=routing_turn,
            ),
            mock.patch.object(
                self.runtime.tool_routing,
                "end_turn",
            ) as routing_end,
        ):
            result = asyncio.run(
                self.runtime.abort_turn(
                    session_id="s1",
                    channel="console",
                )
            )

        self.assertTrue(result["aborted"])
        self.assertNotIn(key, self.runtime._prompt_turn_inputs)
        self.assertNotIn(key, self.runtime._prompt_turn_metadata)
        self.assertNotIn(key, self.runtime._prompt_provider_messages)
        end_turn.assert_awaited_once_with(
            channel="console",
            session_id="s1",
            turn_id="t1",
            commit=False,
        )
        routing_end.assert_called_once_with(
            session_id="s1",
            channel="console",
        )

    def test_failed_prompt_commit_is_aborted_before_bookkeeping_is_dropped(
        self,
    ) -> None:
        calls = []

        class PromptService:
            enabled = True

            async def end_turn(self, **kwargs):
                calls.append(kwargs)
                if kwargs["commit"]:
                    raise ValueError("commit failed")
                return {"committed": False}

        key = ("console", "s1", "t1")
        self.runtime._prompt_turn_inputs[key] = {"value": True}
        self.runtime._prompt_turn_metadata[key] = {"value": True}
        self.runtime._prompt_provider_messages[key] = [
            {"role": "user", "content": "hello"}
        ]

        with (
            mock.patch.object(
                self.runtime,
                "active_prompt_runtime",
                return_value=PromptService(),
            ),
            mock.patch.object(
                self.runtime.tool_routing,
                "current_turn",
                return_value=SimpleNamespace(turn_id="t1"),
            ),
        ):
            asyncio.run(
                self.runtime.memory_after_turn(
                    "hello",
                    "world",
                    session_id="s1",
                    scope={"channel": "console"},
                )
            )

        self.assertTrue(calls[0]["commit"])
        self.assertFalse(calls[1]["commit"])
        self.assertNotIn(key, self.runtime._prompt_turn_inputs)
        self.assertNotIn(key, self.runtime._prompt_turn_metadata)
        self.assertNotIn(key, self.runtime._prompt_provider_messages)

    def test_dynamic_tool_catalog_refreshes_only_between_active_turns(self) -> None:
        class OfflineEmbeddings:
            async def embed(self, _texts, *, input_type):
                raise OSError("offline")

        async def run() -> None:
            self.runtime.tools.register("echo", EchoCapability())
            self.runtime.sync_tool_catalog()
            self.runtime.tool_routing.router.client = OfflineEmbeddings()
            with mock.patch.object(
                self.runtime,
                "refresh_dynamic_tools",
                new=mock.AsyncMock(),
            ) as refresh:
                await self.runtime.prepare_tool_model_request(
                    {"messages": [{"role": "user", "content": "one"}]},
                    session_id="s1",
                    turn_id="t1",
                    channel="console",
                )
                await self.runtime.prepare_tool_model_request(
                    {"messages": [{"role": "user", "content": "one"}]},
                    session_id="s1",
                    turn_id="t1",
                    channel="console",
                )
                await self.runtime.prepare_tool_model_request(
                    {"messages": [{"role": "user", "content": "two"}]},
                    session_id="s2",
                    turn_id="t2",
                    channel="telegram",
                )

            refresh.assert_awaited_once()

        asyncio.run(run())

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
            for cap_id in (
                "filesystem",
                "editor",
                "shell",
                "web.search",
                "web.fetch",
            ):
                if CAPABILITY_ROOTS[cap_id].is_dir():
                    self.assertTrue(self.runtime.capabilities.has(cap_id))
            if CAPABILITY_ROOTS["gateway"].is_dir():
                self.assertTrue(self.runtime.services.has("gateway"))
            if CAPABILITY_ROOTS["security.policy"].is_dir():
                self.assertTrue(self.runtime.policies.has("security.policy"))
            if CAPABILITY_ROOTS["memory.loop"].is_dir():
                self.assertTrue(self.runtime.services.has("memory.loop"))
                self.assertIsNotNone(self.runtime.active_memory_loop())
            if CAPABILITY_ROOTS["prompts.runtime"].is_dir():
                self.assertTrue(self.runtime.services.has("prompts.runtime"))
                self.assertIsNotNone(self.runtime.active_prompt_runtime())
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

    def test_info_logging_does_not_print_startup_extension_inventory(self) -> None:
        output = io.StringIO()
        logger = logging.getLogger("corax.test.quiet-start")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler(output))
        runtime = CoraxRuntime(self.config, logger)
        try:
            asyncio.run(runtime.start())
        finally:
            asyncio.run(runtime.stop())
            logger.handlers.clear()
        startup_log = output.getvalue()
        self.assertNotIn("runtime started:", startup_log)
        self.assertNotIn("agent-core kernel started:", startup_log)

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
                "web.fetch",
                "subagents.delegate",
            ],
        )
        self.assertEqual(status.registry_counts["model_provider"], 3)
        self.assertEqual(
            status.active_by_kind["runtime_service"],
            [
                "gateway",
                "prompts.runtime",
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
        asyncio.run(self.runtime.reload_config(new_config))
        self.assertTrue(self.runtime.running)
        self.assertEqual(len(self.runtime.connectors), 0)

    def test_invalid_reload_leaves_running_composition_untouched(self) -> None:
        asyncio.run(self.runtime.start())
        previous_config = self.runtime.config
        previous_echo = self.runtime.tools.get("echo")
        invalid = cfg.default_config()
        invalid.extensions.active["tool"].append("echo")

        with self.assertRaisesRegex(ValueError, "active in multiple roles"):
            asyncio.run(self.runtime.reload_config(invalid))

        self.assertTrue(self.runtime.running)
        self.assertIs(self.runtime.config, previous_config)
        self.assertIs(self.runtime.tools.get("echo"), previous_echo)

    def test_failed_reload_restores_last_running_config(self) -> None:
        config = cfg.default_config()
        for kind in config.extensions.active:
            config.extensions.active[kind] = []
        config.extensions.active["tool"] = ["echo"]
        config.extensions.bindings = {
            role: "" for role in config.extensions.bindings
        }
        runtime = CoraxRuntime(config)
        asyncio.run(runtime.start())
        previous_echo = runtime.tools.get("echo")
        candidate = cfg.default_config()
        for kind in candidate.extensions.active:
            candidate.extensions.active[kind] = []
        candidate.extensions.active["tool"] = ["echo"]
        candidate.extensions.bindings = {
            role: "" for role in candidate.extensions.bindings
        }
        candidate.agent.name = "rejected"
        original_wire = runtime._wire_runtime_services

        def wire_runtime_services() -> None:
            if runtime.config is candidate:
                raise RuntimeError("candidate wiring failed")
            original_wire()

        try:
            with mock.patch.object(
                runtime,
                "_wire_runtime_services",
                side_effect=wire_runtime_services,
            ):
                with self.assertRaisesRegex(RuntimeError, "candidate wiring failed"):
                    asyncio.run(runtime.reload_config(candidate))
            self.assertTrue(runtime.running)
            self.assertIs(runtime.config, config)
            self.assertIsNot(runtime.tools.get("echo"), previous_echo)
            self.assertEqual(runtime.config.agent.name, config.agent.name)
        finally:
            asyncio.run(runtime.stop())

    def test_unknown_provider_is_skipped(self) -> None:
        self.config.extensions.active["model_provider"] = ["openai"]
        self.config.extensions.bindings["planner"] = "openai"
        asyncio.run(self.runtime.start())
        self.assertFalse(self.runtime.providers.has("openai"))
        self.assertEqual(len(self.runtime.providers), 0)

    def test_broken_extension_is_isolated_and_cleaned_up(self) -> None:
        from agent_core import ExtensionKind
        import corax.runtime as runtime_module

        events: list[str] = []

        class Broken:
            id = "broken"
            kind = ExtensionKind.TOOL

            async def start(self):
                events.append("start")
                raise RuntimeError("boom")

            async def stop(self):
                events.append("stop")

        config = cfg.default_config()
        for kind in config.extensions.active:
            config.extensions.active[kind] = []
        config.extensions.available["broken"] = cfg.ExtensionSpec(kind="tool")
        config.extensions.active["tool"] = ["broken", "echo"]
        with mock.patch.dict(runtime_module._BUILTIN_FACTORIES, {"broken": Broken}):
            runtime = CoraxRuntime(config)
            asyncio.run(runtime.start())
            try:
                self.assertTrue(runtime.running)
                self.assertFalse(runtime.tools.has("broken"))
                self.assertTrue(runtime.tools.has("echo"))
                self.assertEqual(events, ["start", "stop"])
            finally:
                asyncio.run(runtime.stop())

    def test_hung_extension_start_times_out_without_blocking_host(self) -> None:
        from agent_core import ExtensionKind
        import corax.runtime as runtime_module

        events: list[str] = []

        class Hung:
            id = "hung"
            kind = ExtensionKind.TOOL

            async def start(self):
                events.append("start")
                await asyncio.Event().wait()

            async def stop(self):
                events.append("stop")

        config = cfg.default_config()
        for kind in config.extensions.active:
            config.extensions.active[kind] = []
        config.extensions.available["hung"] = cfg.ExtensionSpec(kind="tool")
        config.extensions.active["tool"] = ["hung", "echo"]
        config.limits.task_timeout_seconds = 0.01
        with mock.patch.dict(runtime_module._BUILTIN_FACTORIES, {"hung": Hung}):
            runtime = CoraxRuntime(config)
            asyncio.run(runtime.start())
            try:
                self.assertTrue(runtime.running)
                self.assertFalse(runtime.tools.has("hung"))
                self.assertTrue(runtime.tools.has("echo"))
                self.assertEqual(events, ["start", "stop"])
            finally:
                asyncio.run(runtime.stop())

    def test_cancelled_start_stops_current_and_prior_extensions(self) -> None:
        from agent_core import ExtensionKind
        import corax.runtime as runtime_module

        events: list[str] = []
        entered = asyncio.Event()

        class Fast:
            id = "fast"
            kind = ExtensionKind.TOOL

            async def start(self):
                events.append("start fast")

            async def stop(self):
                events.append("stop fast")

        class Hanging:
            id = "hanging"
            kind = ExtensionKind.TOOL

            async def start(self):
                events.append("start hanging")
                entered.set()
                await asyncio.Event().wait()

            async def stop(self):
                events.append("stop hanging")

        config = cfg.default_config()
        for kind in config.extensions.active:
            config.extensions.active[kind] = []
        config.extensions.available["fast"] = cfg.ExtensionSpec(kind="tool")
        config.extensions.available["hanging"] = cfg.ExtensionSpec(kind="tool")
        config.extensions.active["tool"] = ["fast", "hanging"]
        config.extensions.bindings = {
            role: "" for role in config.extensions.bindings
        }
        runtime = CoraxRuntime(config)

        async def cancel_during_start() -> None:
            task = asyncio.create_task(runtime.start())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with mock.patch.dict(
            runtime_module._BUILTIN_FACTORIES,
            {"fast": Fast, "hanging": Hanging},
        ):
            asyncio.run(cancel_during_start())

        self.assertEqual(
            events,
            ["start fast", "start hanging", "stop hanging", "stop fast"],
        )
        self.assertFalse(runtime.running)
        self.assertEqual(len(runtime.extensions), 0)
        self.assertEqual(runtime._started_extensions, [])

    def test_half_disabled_matrix_keeps_builtin_agent_operational(self) -> None:
        from agent_core import TaskStatus

        for parity in (0, 1):
            with self.subTest(parity=parity), tempfile.TemporaryDirectory() as tmp:
                config = cfg.default_config()
                for kind, extension_ids in config.extensions.active.items():
                    config.extensions.active[kind] = extension_ids[parity::2]
                for kind, extension_id in (
                    ("tool", "echo"),
                    ("model_provider", "stub"),
                    ("memory_provider", "memory.none"),
                ):
                    if extension_id not in config.extensions.active[kind]:
                        config.extensions.active[kind].append(extension_id)
                active_ids = {
                    extension_id
                    for extension_ids in config.extensions.active.values()
                    for extension_id in extension_ids
                }
                for role, extension_id in config.extensions.bindings.items():
                    if extension_id not in active_ids:
                        config.extensions.bindings[role] = ""
                for extension_id, spec in config.extensions.available.items():
                    if spec.path:
                        spec.path = str(Path(tmp) / "missing" / extension_id)

                runtime = CoraxRuntime(config, root_path=tmp)
                with mock.patch.dict(os.environ, {}, clear=True):
                    asyncio.run(runtime.start())
                    try:
                        plan = asyncio.run(
                            runtime.invoke_extension(
                                "stub",
                                {"operation": "plan", "prompt": "ping"},
                            )
                        )
                        task = asyncio.run(
                            runtime.execute(
                                "echo",
                                input={"text": "ping"},
                            )
                        )
                        self.assertTrue(runtime.running)
                        self.assertEqual(plan["tasks"][0]["capability"], "echo")
                        self.assertIs(task.status, TaskStatus.COMPLETED)
                    finally:
                        asyncio.run(runtime.stop())

    def test_generation_model_falls_back_from_non_generator_binding(self) -> None:
        from agent_core import ExtensionKind

        class Generator:
            id = "generate.test"
            kind = ExtensionKind.MODEL_PROVIDER

            async def generate(self, request):
                return None

        self.runtime.models.register("stub", StubPlanner())
        self.runtime.models.register("generate.test", Generator())
        self.config.extensions.bindings["primary_model"] = "stub"
        self.assertEqual(
            self.runtime.active_generation_model_id(),
            "generate.test",
        )

    def test_reload_recomputes_paths_and_runtime_owned_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = cfg.default_config()
            for kind in config.extensions.active:
                config.extensions.active[kind] = []
            config.extensions.active["tool"] = ["echo"]
            config.runtime.workspace_path = "old-work"
            config.runtime.data_path = "old-data"
            runtime = CoraxRuntime(config, root_path=tmp)

            updated = cfg.default_config()
            for kind in updated.extensions.active:
                updated.extensions.active[kind] = []
            updated.extensions.active["tool"] = ["echo"]
            updated.extensions.bindings = {
                role: "" for role in updated.extensions.bindings
            }
            updated.runtime.workspace_path = "new-work"
            updated.runtime.data_path = "new-data"

            with mock.patch.dict(os.environ, {}, clear=True):
                asyncio.run(runtime.start())
                asyncio.run(runtime.reload_config(updated))
                try:
                    self.assertEqual(
                        runtime.workspace_path,
                        (Path(tmp) / "new-work").resolve(),
                    )
                    self.assertEqual(
                        runtime.data_path,
                        (Path(tmp) / "new-data").resolve(),
                    )
                    self.assertEqual(
                        runtime.extension_loader.workspace_path,
                        runtime.workspace_path,
                    )
                    self.assertEqual(
                        os.environ["CORAX_STATE_PATH"],
                        str((Path(tmp) / "new-data" / "state").resolve()),
                    )
                finally:
                    asyncio.run(runtime.stop())

    def test_memory_service_failure_degrades_without_breaking_turn(self) -> None:
        from agent_core import ExtensionKind

        class BrokenMemoryLoop:
            id = "memory.broken"
            kind = ExtensionKind.RUNTIME_SERVICE

            async def handle(self, request):
                raise RuntimeError("memory unavailable")

        self.runtime.services.register("memory.broken", BrokenMemoryLoop())
        self.config.extensions.bindings["memory_loop"] = "memory.broken"
        before = asyncio.run(
            self.runtime.memory_before_turn("hello", session_id="s")
        )
        after = asyncio.run(
            self.runtime.memory_after_turn("hello", "world", session_id="s")
        )
        self.assertTrue(before["degraded"])
        self.assertFalse(after["stored"])

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

    def test_start_exports_security_blocked_paths(self) -> None:
        import os

        old_value = os.environ.get("CORAX_SECURITY_BLOCKED_PATHS")
        config = cfg.default_config()
        config.security.blocked_paths = ["~/.ssh", ".env"]
        runtime = CoraxRuntime(config)
        asyncio.run(runtime.start())
        try:
            self.assertEqual(
                os.environ["CORAX_SECURITY_BLOCKED_PATHS"],
                "~/.ssh,.env",
            )
        finally:
            asyncio.run(runtime.stop())
            if old_value is None:
                os.environ.pop("CORAX_SECURITY_BLOCKED_PATHS", None)
            else:
                os.environ["CORAX_SECURITY_BLOCKED_PATHS"] = old_value

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

    def test_every_model_request_gets_fresh_trusted_local_time(self) -> None:
        from agent_core import ExtensionKind

        kst = timezone(timedelta(hours=9), "KST")
        moments = iter(
            [
                datetime(2026, 7, 26, 23, 59, 58, tzinfo=kst),
                datetime(2026, 7, 27, 0, 0, 1, tzinfo=kst),
                datetime(2026, 7, 27, 0, 0, 2, tzinfo=kst),
            ]
        )

        class Model:
            id = "clock.test"
            kind = ExtensionKind.MODEL_PROVIDER

            def __init__(self) -> None:
                self.requests = []

            async def generate(self, request):
                self.requests.append(request)
                return SimpleNamespace(
                    is_success=True,
                    payload={"text": "ok"},
                    status=None,
                )

            async def stream_generate_events(self, request):
                self.requests.append(request)
                yield {"type": "done"}

        runtime = CoraxRuntime(self.config, clock=lambda: next(moments))
        model = Model()
        runtime.models.register(model.id, model)
        payload = {
            "messages": [
                {"role": "system", "content": "base policy"},
                {"role": "user", "content": "what is current?"},
            ]
        }
        asyncio.run(runtime.invoke_extension(model.id, payload))
        asyncio.run(runtime.invoke_extension(model.id, payload))

        async def stream() -> None:
            async for _event in runtime.stream_extension(model.id, payload):
                pass

        asyncio.run(stream())

        blocks = [request.messages[0]["content"] for request in model.requests]
        self.assertEqual(len(blocks), 3)
        self.assertIn("Local date: 2026-07-26", blocks[0])
        self.assertIn("Local time: 23:59:58", blocks[0])
        self.assertIn("Local date: 2026-07-27", blocks[1])
        self.assertIn("Local time: 00:00:02", blocks[2])
        self.assertIn("Timezone: KST (UTC+09:00)", blocks[0])
        self.assertIn("web search is required", blocks[0])
        self.assertIn("source URLs", blocks[0])
        self.assertIn("Never guess current facts", blocks[0])
        self.assertEqual(payload["messages"][0]["content"], "base policy")
        self.assertFalse(
            any(
                "Trusted Corax runtime context" in message["content"]
                for message in payload["messages"]
            )
        )
        for request in model.requests:
            self.assertTrue(request.messages[0]["content"].startswith("base policy"))
            self.assertIn(
                "Trusted Corax runtime context",
                request.messages[0]["content"],
            )
            self.assertEqual(
                [message.get("role") for message in request.messages].count("system"),
                1,
            )

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
                    "scope": "prepared",
                    "source": "host",
                },
                {"type": "delta", "content": "ok"},
                {"type": "done"},
            ],
        )

    def test_provider_preflight_counts_final_request_for_batch_and_stream(self) -> None:
        from agent_core import ExtensionKind

        order = []

        class ContextManager:
            id = "context.manager"
            kind = ExtensionKind.RUNTIME_SERVICE
            max_chars = 48_000

            async def handle(self, request):
                messages = list(request.payload["messages"])
                messages.insert(
                    -1,
                    {"role": "system", "content": "compacted-history-marker"},
                )
                return SimpleNamespace(
                    is_success=True,
                    payload={
                        "messages": messages,
                        "chars_after": 137,
                        "overflow": True,
                    },
                )

        class Model:
            id = "preflight.test"
            kind = ExtensionKind.MODEL_PROVIDER

            def __init__(self) -> None:
                self.counted = []
                self.generated = []

            async def count_tokens(self, request):
                order.append("count")
                self.counted.append(request)
                return (
                    2_222
                    if any(
                        "compacted-history-marker"
                        in str(message.get("content", ""))
                        for message in request.messages
                    )
                    else 127_000
                )

            async def generate(self, request):
                order.append("generate")
                self.generated.append(request)
                return SimpleNamespace(
                    is_success=True,
                    payload={"text": "ok"},
                    status=None,
                )

            async def stream_generate_events(self, request):
                order.append("stream")
                self.generated.append(request)
                yield {"type": "done"}

        model = Model()
        self.runtime.services.register("context.manager", ContextManager())
        self.runtime.models.register(model.id, model)
        self.runtime.set_model_context_window(131_072)
        payload = {
            "messages": [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "second question"},
            ],
            "max_tokens": 4_096,
        }

        asyncio.run(self.runtime.invoke_extension(model.id, payload))

        async def collect() -> list[dict]:
            return [
                event
                async for event in self.runtime.stream_extension(
                    model.id,
                    payload,
                )
            ]

        events = asyncio.run(collect())
        self.assertEqual(
            order,
            ["count", "count", "generate", "count", "count", "stream"],
        )
        self.assertIs(model.counted[1], model.generated[0])
        self.assertIs(model.counted[3], model.generated[1])
        for request in (model.counted[1], model.counted[3]):
            self.assertTrue(
                any(
                    "compacted-history-marker"
                    in str(message.get("content", ""))
                    for message in request.messages
                )
            )
            self.assertTrue(
                any(
                    str(message.get("content", "")).startswith(
                        "Trusted Corax runtime context"
                    )
                    for message in request.messages
                )
            )
        self.assertEqual(
            events,
            [
                {
                    "type": "context",
                    "used": 2_222,
                    "limit": 131_072,
                    "unit": "tokens",
                    "scope": "prompt",
                    "source": "provider",
                },
                {"type": "done"},
            ],
        )

    def test_exact_preflight_preserves_full_history_when_it_fits(self) -> None:
        from agent_core import ExtensionKind

        class ContextManager:
            id = "context.manager"
            kind = ExtensionKind.RUNTIME_SERVICE

            async def handle(self, request):
                raise AssertionError("exact-fit history must not be compacted")

        class Model:
            id = "preflight.full-history"
            kind = ExtensionKind.MODEL_PROVIDER

            def __init__(self) -> None:
                self.request = None

            async def count_tokens(self, request):
                return 2_222

            async def generate(self, request):
                self.request = request
                return SimpleNamespace(
                    is_success=True,
                    payload={"text": "ok"},
                    status=None,
                )

        model = Model()
        self.runtime.services.register("context.manager", ContextManager())
        self.runtime.models.register(model.id, model)
        self.runtime.set_model_context_window(131_072)
        history = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
        ]

        asyncio.run(
            self.runtime.invoke_extension(
                model.id,
                {"messages": history},
            )
        )

        contents = [message.get("content") for message in model.request.messages]
        for message in history:
            self.assertIn(message["content"], contents)

    def test_provider_preflight_overflow_stops_batch_and_stream(self) -> None:
        from agent_core import ExtensionKind

        class ContextManager:
            id = "context.manager"
            kind = ExtensionKind.RUNTIME_SERVICE
            max_chars = 48_000

            async def handle(self, request):
                return SimpleNamespace(
                    is_success=True,
                    payload={
                        "messages": list(request.payload["messages"]),
                        "chars_after": 137,
                    },
                )

        class Model:
            id = "preflight.overflow"
            kind = ExtensionKind.MODEL_PROVIDER

            def __init__(self) -> None:
                self.model_calls = 0
                self.count_calls = 0

            async def count_tokens(self, request):
                self.count_calls += 1
                return 5_000 if self.count_calls % 2 else None

            async def generate(self, request):
                self.model_calls += 1
                raise AssertionError("oversized request reached the model")

            async def stream_generate_events(self, request):
                self.model_calls += 1
                yield {"type": "done"}

        model = Model()
        self.runtime.services.register("context.manager", ContextManager())
        self.runtime.models.register(model.id, model)
        self.runtime.set_model_context_window(10_000)
        payload = {
            "messages": [{"role": "user", "content": "too large"}],
        }

        with self.assertRaisesRegex(RuntimeError, "exceeds"):
            asyncio.run(self.runtime.invoke_extension(model.id, payload))

        async def collect() -> None:
            async for _event in self.runtime.stream_extension(model.id, payload):
                pass

        with self.assertRaisesRegex(RuntimeError, "exceeds"):
            asyncio.run(collect())
        self.assertEqual(model.model_calls, 0)

    def test_plan_skips_model_request_token_preflight(self) -> None:
        from agent_core import ExtensionKind

        class Model:
            id = "preflight.plan"
            kind = ExtensionKind.MODEL_PROVIDER

            async def count_tokens(self, request):
                raise AssertionError("plan sends a different request contract")

            async def plan(self, request):
                return SimpleNamespace(
                    is_success=True,
                    payload={"plan": "ok"},
                    status=None,
                )

        self.runtime.models.register(Model.id, Model())
        self.runtime.set_model_context_window(131_072)
        result = asyncio.run(
            self.runtime.invoke_extension(
                Model.id,
                {"operation": "plan", "prompt": "make a plan"},
            )
        )
        self.assertEqual(result, {"plan": "ok"})

    def test_provider_preflight_failure_keeps_host_fallback(self) -> None:
        from agent_core import ExtensionKind

        class ContextManager:
            id = "context.manager"
            kind = ExtensionKind.RUNTIME_SERVICE
            max_chars = 48_000

            async def handle(self, request):
                return SimpleNamespace(
                    is_success=True,
                    payload={
                        "messages": list(request.payload["messages"]),
                        "chars_after": 137,
                    },
                )

        class Model:
            kind = ExtensionKind.MODEL_PROVIDER

            def __init__(self, model_id, failure):
                self.id = model_id
                self.failure = failure

            async def count_tokens(self, request):
                if self.failure is not None:
                    raise self.failure
                return None

            async def stream_generate_events(self, request):
                yield {"type": "done"}

        class ModelWithoutCounter:
            id = "preflight.missing"
            kind = ExtensionKind.MODEL_PROVIDER

            async def stream_generate_events(self, request):
                yield {"type": "done"}

        self.runtime.services.register("context.manager", ContextManager())
        self.runtime.set_model_context_window(131_072)
        models = (
            ModelWithoutCounter(),
            Model("preflight.none", None),
            Model("preflight.error", RuntimeError("tokenizer unavailable")),
        )
        for model in models:
            self.runtime.models.register(model.id, model)

        async def collect(model_id) -> list[dict]:
            return [
                event
                async for event in self.runtime.stream_extension(
                    model_id,
                    {"messages": [{"role": "user", "content": "hello"}]},
                )
            ]

        with self.assertLogs("corax.runtime", level="DEBUG") as logs:
            results = [asyncio.run(collect(model.id)) for model in models]

        for events in results:
            self.assertEqual(events[0]["source"], "host")
            self.assertEqual(events[0]["used"], 137)
            self.assertEqual(events[-1], {"type": "done"})
        self.assertIn("returned no count", "\n".join(logs.output))
        self.assertIn("tokenizer unavailable", "\n".join(logs.output))

    def test_model_window_budget_uses_provider_prompt_calibration(self) -> None:
        from agent_core import ExtensionKind

        manager_requests = []

        class ContextManager:
            id = "context.manager"
            kind = ExtensionKind.RUNTIME_SERVICE
            max_chars = 48_000

            async def handle(self, request):
                manager_requests.append(dict(request.payload))
                return SimpleNamespace(
                    is_success=True,
                    payload={
                        "messages": list(request.payload["messages"]),
                        "chars_after": 100,
                        "bytes_after": 200,
                        "prompt_bytes_after": 240,
                        "budget_limit": 100_000,
                        "budget_unit": "bytes",
                    },
                )

        class StreamingModel:
            id = "stream.window"
            kind = ExtensionKind.MODEL_PROVIDER

            async def stream_generate_events(self, request):
                yield {
                    "type": "context",
                    "used": 120,
                    "unit": "tokens",
                    "scope": "prompt",
                    "source": "provider",
                }
                yield {"type": "done"}

        self.runtime.services.register("context.manager", ContextManager())
        self.runtime.models.register("stream.window", StreamingModel())
        self.runtime.set_model_context_window(131_072)
        payload = {
            "messages": [{"role": "user", "content": "hello"}],
            "model": "qwen",
            "max_tokens": 4_096,
            "tools": [{"type": "function", "function": {"name": "echo"}}],
            "tool_choice": "auto",
        }

        async def run() -> None:
            for _ in range(2):
                async for _event in self.runtime.stream_extension(
                    "stream.window",
                    payload,
                    session_id="window-stream",
                ):
                    pass

        asyncio.run(run())

        first, second = manager_requests
        self.assertEqual(first["context_window_tokens"], 131_072)
        self.assertEqual(first["reserved_tokens"], 5_120)
        self.assertGreater(first["prompt_overhead_bytes"], 0)
        self.assertIsNone(first["observed_bytes_per_token"])
        self.assertEqual(second["observed_bytes_per_token"], 2.0)
        for request in manager_requests:
            self.assertTrue(
                any(
                    str(message.get("content", "")).startswith(
                        "Trusted Corax runtime context"
                    )
                    for message in request["messages"]
                )
            )

    def test_context_overflow_fails_before_model_request(self) -> None:
        from agent_core import ExtensionKind

        class ContextManager:
            id = "context.manager"
            kind = ExtensionKind.RUNTIME_SERVICE

            async def handle(self, request):
                return SimpleNamespace(
                    is_success=True,
                    payload={
                        "messages": list(request.payload["messages"]),
                        "overflow": True,
                    },
                )

        class Model:
            id = "overflow.test"
            kind = ExtensionKind.MODEL_PROVIDER

            async def generate(self, request):
                raise AssertionError("model must not receive oversized context")

        self.runtime.services.register("context.manager", ContextManager())
        self.runtime.models.register("overflow.test", Model())

        with self.assertRaisesRegex(RuntimeError, "exceeds"):
            asyncio.run(
                self.runtime.invoke_extension(
                    "overflow.test",
                    {"messages": [{"role": "user", "content": "too large"}]},
                )
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
