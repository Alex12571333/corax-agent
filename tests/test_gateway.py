"""Gateway: tool-calling loop, command dispatch, resilient loop, and policy.

The loop logic is exercised with injected fakes — no kernel, no network — so the
tests are fast and deterministic.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from corax.gateway import CoraxTelegramGateway, GatewayError
from corax.gateway.policy import GatewayPolicyEngine

REPO_ROOT = Path(__file__).resolve().parents[1]
TELEGRAM_REPO = REPO_ROOT.parent / "corax-telegram-connector"

try:
    import agent_core  # noqa: F401
    import agent_sdk  # noqa: F401

    HAS_CORE = True
except ImportError:  # pragma: no cover
    HAS_CORE = False

CAPS = [
    {"id": "filesystem", "description": "fs", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    {"id": "shell", "description": "shell", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}},
    {"id": "clock", "input_schema": {}},      # empty schema + no description
    {"description": "no id"},                  # skipped (no id)
    {"id": "llm.local", "input_schema": {}},   # excluded (infra)
    {"id": "telegram.connector", "input_schema": {}},  # excluded (infra)
]


class FakeBackend:
    def __init__(self, poll_batches=None, llm_responses=None, tool_results=None):
        self.poll_batches = list(poll_batches or [])
        self.llm_responses = list(llm_responses or [])
        self.tool_results = dict(tool_results or {})
        self.calls: list = []
        self.sends: list[str] = []
        self.tools_run: list = []
        self.fail_capability: str | None = None

    async def run_capability(self, cap_id, payload, *, session_id=None):
        op = payload.get("operation")
        self.calls.append((cap_id, op, payload))
        if cap_id == self.fail_capability:
            raise GatewayError("boom")
        if op == "poll":
            return {"updates": self.poll_batches.pop(0) if self.poll_batches else [], "next_offset": 999}
        if op == "send":
            self.sends.append(payload["text"])
            return {"message_id": 1}
        if cap_id == "llm.local" and op == "generate":
            resp = self.llm_responses.pop(0) if self.llm_responses else {"text": "(default)"}
            tcs = resp.get("tool_calls") or []
            return {"text": resp.get("text", ""), "tool_calls": tcs,
                    "finish_reason": "tool_calls" if tcs else "stop"}
        self.tools_run.append((cap_id, payload))  # a tool invocation
        return self.tool_results.get(cap_id, {"ok": True})


def _tool_call(name, arguments="{}", id="c1"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _text_update(chat_id, text):
    return {"chat_id": chat_id, "text": text, "command": {"is_command": False}}


def _cmd_update(chat_id, command, args="", reply=None):
    return {"chat_id": chat_id, "text": f"/{command}",
            "command": {"is_command": True, "command": command, "args": args, "reply": reply}}


async def _nosleep(_seconds):
    return None


def _gateway(backend, **kwargs):
    kwargs.setdefault("capabilities", CAPS)
    return CoraxTelegramGateway(
        run_capability=backend.run_capability,
        sleep=_nosleep,
        new_session=lambda: "sess-fixed",
        **kwargs,
    )


class ToolSpecTests(unittest.TestCase):
    def test_tools_built_from_capabilities_excluding_infra(self) -> None:
        gw = _gateway(FakeBackend())
        names = {t["function"]["name"] for t in gw._tool_specs}
        self.assertEqual(names, {"filesystem", "shell", "clock"})  # llm/telegram excluded, no-id skipped
        self.assertEqual(gw._tool_to_cap["filesystem"], "filesystem")
        clock = next(t for t in gw._tool_specs if t["function"]["name"] == "clock")
        self.assertEqual(clock["function"]["description"], "clock")  # falls back to id
        self.assertEqual(clock["function"]["parameters"], {"type": "object", "properties": {}})

    def test_dotted_ids_become_safe_tool_names(self) -> None:
        gw = _gateway(FakeBackend(), capabilities=[{"id": "web.search", "input_schema": {}}])
        self.assertIn("web_search", gw._tool_to_cap)
        self.assertEqual(gw._tool_to_cap["web_search"], "web.search")


class ChatToolLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_answer_no_tools(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]],
                              llm_responses=[{"text": "hello there"}])
        gw = _gateway(backend, model="gemma-4")
        await gw.run(max_iterations=1)
        self.assertIn("hello there", backend.sends)
        gen = next(p for c, op, p in backend.calls if op == "generate")
        self.assertEqual(gen["model"], "gemma-4")
        self.assertTrue(gen["tools"])  # tools were offered

    async def test_tool_call_then_final_answer(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "list files")]],
            llm_responses=[
                {"tool_calls": [_tool_call("filesystem", '{"path": "."}')]},
                {"text": "here are your files"},
            ],
            tool_results={"filesystem": {"files": ["a.txt"]}},
        )
        gw = _gateway(backend)
        await gw.run(max_iterations=1)
        self.assertEqual(backend.tools_run[0][0], "filesystem")
        self.assertEqual(backend.tools_run[0][1], {"path": "."})  # args parsed, op stripped
        self.assertIn("here are your files", backend.sends)

    async def test_unknown_tool_is_fed_back_as_error(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "x")]],
            llm_responses=[{"tool_calls": [_tool_call("nope")]}, {"text": "ok"}],
        )
        gw = _gateway(backend)
        await gw.run(max_iterations=1)
        # second generate call carries a tool message with the error
        gen_calls = [p for c, op, p in backend.calls if op == "generate"]
        tool_msg = gen_calls[1]["messages"][-1]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertIn("unknown tool", tool_msg["content"])

    async def test_bad_arguments_default_to_empty(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "x")]],
            llm_responses=[{"tool_calls": [_tool_call("filesystem", "{bad json")]}, {"text": "done"}],
        )
        gw = _gateway(backend)
        await gw.run(max_iterations=1)
        self.assertEqual(backend.tools_run[0][1], {})  # invalid JSON -> {}

    async def test_non_dict_arguments_default_to_empty(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "x")]],
            llm_responses=[{"tool_calls": [_tool_call("shell", "[1,2,3]")]}, {"text": "done"}],
        )
        gw = _gateway(backend)
        await gw.run(max_iterations=1)
        self.assertEqual(backend.tools_run[0][1], {})

    async def test_failing_tool_error_fed_back(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "x")]],
            llm_responses=[{"tool_calls": [_tool_call("filesystem")]}, {"text": "ok"}],
        )
        backend.fail_capability = "filesystem"
        gw = _gateway(backend)
        await gw.run(max_iterations=1)
        gen_calls = [p for c, op, p in backend.calls if op == "generate"]
        tool_msg = gen_calls[1]["messages"][-1]
        self.assertIn("error", tool_msg["content"])

    async def test_max_tool_iterations_stops(self) -> None:
        # The model always asks for another tool — the loop must bail out.
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "x")]],
            llm_responses=[{"tool_calls": [_tool_call("clock")]}] * 10,
        )
        gw = _gateway(backend, max_tool_iterations=3)
        await gw.run(max_iterations=1)
        self.assertTrue(any("too many tool steps" in s for s in backend.sends))

    async def test_no_tools_when_no_capabilities(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]],
                              llm_responses=[{"text": "hi back"}])
        gw = _gateway(backend, capabilities=[])
        await gw.run(max_iterations=1)
        gen = next(p for c, op, p in backend.calls if op == "generate")
        self.assertNotIn("tools", gen)

    async def test_empty_final_text_sends_placeholder(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]], llm_responses=[{"text": ""}])
        gw = _gateway(backend)
        await gw.run(max_iterations=1)
        self.assertIn("(no response)", backend.sends)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_session(self) -> None:
        backend = FakeBackend(poll_batches=[[_cmd_update(5, "new_session", reply="🆕")]])
        await _gateway(backend).run(max_iterations=1)
        self.assertIn("🆕", backend.sends)

    async def test_reload(self) -> None:
        backend = FakeBackend(poll_batches=[[_cmd_update(5, "reload_agent", reply="reloading")]])
        outcome = await _gateway(backend).run(max_iterations=5)
        self.assertEqual(outcome, "reload")
        self.assertIn("reloading", backend.sends)

    async def test_set_model(self) -> None:
        backend = FakeBackend(poll_batches=[[_cmd_update(5, "set_model", args="gemma-4"),
                                            _cmd_update(5, "set_model", args="")]])
        gw = _gateway(backend)
        await gw.run(max_iterations=1)
        self.assertEqual(gw.model, "gemma-4")
        self.assertTrue(any("Current model" in s for s in backend.sends))

    async def test_help_cancel_unknown(self) -> None:
        backend = FakeBackend(poll_batches=[[
            _cmd_update(5, "help", reply="HELP"),
            _cmd_update(5, "cancel", reply="🛑"),
            {"chat_id": 5, "text": "/x", "command": {"is_command": True, "command": "unknown"}},
        ]])
        await _gateway(backend).run(max_iterations=1)
        self.assertIn("HELP", backend.sends)
        self.assertIn("🛑", backend.sends)
        self.assertTrue(any("Unknown command" in s for s in backend.sends))

    async def test_command_defaults_when_no_reply(self) -> None:
        backend = FakeBackend(poll_batches=[[
            _cmd_update(5, "new_session"), _cmd_update(5, "help"),
            _cmd_update(5, "cancel"), _cmd_update(5, "reload_agent"),  # reload last (breaks loop)
        ]])
        outcome = await _gateway(backend).run(max_iterations=5)
        self.assertEqual(outcome, "reload")
        self.assertEqual(len(backend.sends), 4)  # all four produced a default reply


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_without_chat_id_skipped(self) -> None:
        backend = FakeBackend(poll_batches=[[{"chat_id": None, "text": "x", "command": {}}]])
        await _gateway(backend).run(max_iterations=1)
        self.assertEqual(backend.sends, [])

    async def test_blank_text_skipped(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "   ")]])
        await _gateway(backend).run(max_iterations=1)
        self.assertFalse([p for c, op, p in backend.calls if op == "generate"])

    async def test_idle_then_stop(self) -> None:
        backend = FakeBackend(poll_batches=[[]])
        outcome = await _gateway(backend).run(max_iterations=1)
        self.assertEqual(outcome, "stopped")

    async def test_stop_breaks_loop(self) -> None:
        backend = FakeBackend(poll_batches=[[_cmd_update(5, "help", reply="x")],
                                            [_cmd_update(5, "help", reply="y")]])
        gw = _gateway(backend)

        original = gw._handle_command

        async def stop_after(chat_id, command):
            gw.stop()
            await original(chat_id, command)

        gw._handle_command = stop_after
        outcome = await gw.run(max_iterations=10)
        self.assertEqual(outcome, "stopped")

    async def test_poll_offset_threaded(self) -> None:
        backend = FakeBackend(poll_batches=[[], []])
        await _gateway(backend).run(max_iterations=2)
        poll_calls = [p for c, op, p in backend.calls if op == "poll"]
        self.assertEqual(poll_calls[1]["offset"], 999)

    async def test_poll_failure_is_logged_not_fatal(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]])
        backend.fail_capability = "telegram.connector"
        outcome = await _gateway(backend).run(max_iterations=1)
        self.assertEqual(outcome, "stopped")

    async def test_update_handling_failure_is_logged_not_fatal(self) -> None:
        # Poll succeeds (no offset yet), but the LLM call fails — survive.
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]])
        backend.fail_capability = "llm.local"
        outcome = await _gateway(backend).run(max_iterations=1)
        self.assertEqual(outcome, "stopped")

    async def test_default_sleep_helper(self) -> None:
        from corax.gateway.telegram_gateway import _async_sleep

        await _async_sleep(0)  # the real idle sleep (0s)


class GatewayPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_allows_all_but_blocked(self) -> None:
        from agent_core import PermissionLevel, RiskLevel, SideEffect
        from agent_core.policy.base import PolicyContext

        policy = GatewayPolicyEngine()

        def ctx(level):
            return PolicyContext(session_id="s", capability_id="c", permission_level=level,
                                 risk_level=RiskLevel.HIGH, side_effects={SideEffect.EXECUTE_CODE},
                                 required_scopes=set())

        self.assertTrue((await policy.evaluate(None, None, ctx(PermissionLevel.CONFIRM))).allowed)
        self.assertTrue((await policy.evaluate(None, None, ctx(PermissionLevel.DANGEROUS))).allowed)
        self.assertFalse((await policy.evaluate(None, None, ctx(PermissionLevel.BLOCKED))).allowed)


class CoreRoundTripTests(unittest.IsolatedAsyncioTestCase):
    """Run a capability through the real kernel under the gateway policy and read
    its output back from core session state."""

    async def asyncSetUp(self) -> None:
        if not HAS_CORE:
            self.skipTest("agent-core / agent-sdk not installed")
        if not TELEGRAM_REPO.is_dir():
            self.skipTest("corax-telegram-connector repo not present")
        from corax import config as cfg
        from corax.runtime import CoraxRuntime

        config = cfg.default_config()
        config.capabilities.available["telegram.connector"].path = str(TELEGRAM_REPO)
        self.runtime = CoraxRuntime(config, root_path=REPO_ROOT)
        await self.runtime.start()

    async def asyncTearDown(self) -> None:
        await self.runtime.stop()

    async def test_invoke_routes_through_core_and_returns_payload(self) -> None:
        async with self.runtime.core.session(
            self.runtime.capabilities, policy=GatewayPolicyEngine()
        ) as kernel:
            output = await kernel.invoke(
                "telegram.connector",
                {"operation": "poll", "mock": True,
                 "mock_updates": [{"update_id": 1, "message": {"text": "/new", "chat": {"id": 7}}}]},
                wait_timeout=10,
            )
        self.assertEqual(output["count"], 1)
        self.assertEqual(output["updates"][0]["command"]["command"], "new_session")

    async def test_invoke_raises_with_reason_on_failure(self) -> None:
        from corax.loader.core import KernelInvocationError

        async with self.runtime.core.session(
            self.runtime.capabilities, policy=GatewayPolicyEngine()
        ) as kernel:
            with self.assertRaises(KernelInvocationError) as ctx:
                await kernel.invoke("telegram.connector", {"operation": "nope"}, wait_timeout=10)
        self.assertIn("unsupported operation", str(ctx.exception))


class EchoWrapperTests(unittest.IsolatedAsyncioTestCase):
    """The kernel wraps every capability so its result round-trips through the
    core — even capabilities that don't echo to state_patch themselves."""

    async def asyncSetUp(self) -> None:
        if not HAS_CORE:
            self.skipTest("agent-core not installed")

    async def test_wrapper_echoes_a_non_echoing_capability(self) -> None:
        import agent_core as ac

        from corax.config import default_config
        from corax.loader.core import CoreEngine, KernelInvocationError

        class FakeCap(ac.Capability):  # a plain capability that never touches state_patch
            id = "fake.tool"
            name = "Fake"
            description = "doubles x"
            version = "1.0.0"
            tags: set = set()
            permission_level = ac.PermissionLevel.SAFE
            required_scopes: set = set()
            risk_level = ac.RiskLevel.LOW
            side_effects = {ac.SideEffect.NONE}
            input_schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
            output_schema = {"type": "object", "properties": {"doubled": {"type": "integer"}}}

            async def execute(self, request):
                x = request.input.get("x", 0)
                if x < 0:  # schema-valid, but the capability rejects it
                    return ac.Result.fail(
                        ac.CoreError(ac.ErrorCode.INVALID_INPUT, "x must be non-negative"),
                        session_id=request.session_id, task_id=request.task_id,
                    )
                return ac.Result.ok({"doubled": x * 2}, session_id=request.session_id,
                                    task_id=request.task_id)

            async def health_check(self):
                return ac.HealthStatus.HEALTHY

        engine = CoreEngine(default_config())
        async with engine.session([("fake.tool", FakeCap())], policy=GatewayPolicyEngine()) as kernel:
            out = await kernel.invoke("fake.tool", {"x": 21}, wait_timeout=10)
            self.assertEqual(out, {"doubled": 42})  # payload echoed by the wrapper
            with self.assertRaises(KernelInvocationError) as ctx:
                await kernel.invoke("fake.tool", {"x": -1}, wait_timeout=10)
            self.assertIn("x must be non-negative", str(ctx.exception))  # error echoed too


if __name__ == "__main__":
    unittest.main()
