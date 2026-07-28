"""Gateway: tool-calling loop, command dispatch, resilient loop, and policy.

The loop logic is exercised with injected fakes — no kernel, no network — so the
tests are fast and deterministic.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from corax.gateway import CoraxTelegramGateway, GatewayError

REPO_ROOT = Path(__file__).resolve().parents[1]
TELEGRAM_REPO = REPO_ROOT.parent / "corax-telegram-connector"
LLM_REPO = REPO_ROOT.parent / "corax-llm-local-connector"

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
CAPS_WITH_GATEWAY = [
    *CAPS,
    {
        "id": "gateway",
        "description": "gateway",
        "input_schema": {"type": "object", "properties": {"operation": {"type": "string"}}},
    },
]
WEB_CAP = {
    "id": "web.search",
    "description": "Search the web and return titles, URLs, and snippets.",
    "input_schema": {
        "type": "object",
        "properties": {
            "operation": {"type": "string"},
            "query": {"type": "string"},
            "count": {"type": "integer"},
        },
    },
}


class FakeBackend:
    def __init__(self, poll_batches=None, llm_responses=None, tool_results=None):
        self.poll_batches = list(poll_batches or [])
        self.llm_responses = list(llm_responses or [])
        self.tool_results = dict(tool_results or {})
        self.calls: list = []
        self.sends: list[str] = []
        self.documents: list[dict] = []
        self.tools_run: list = []
        self.policy_calls: list[tuple[str, str | None, dict | None]] = []
        self.gateway_calls: list[dict] = []
        self.fail_capability: str | None = None
        self.fail_operation: str | None = None
        self.confirm_capability: str | None = None

    async def run_capability(
        self,
        cap_id,
        payload,
        *,
        session_id=None,
        policy_metadata=None,
    ):
        op = payload.get("operation")
        self.calls.append((cap_id, op, payload))
        if policy_metadata is not None:
            self.policy_calls.append(
                (cap_id, session_id, dict(policy_metadata))
            )
        if cap_id == self.fail_capability or (self.fail_operation is not None and op == self.fail_operation):
            raise GatewayError("boom")
        if cap_id == self.confirm_capability:
            error = GatewayError("confirmation required")
            error.task_id = "task-confirm-1"
            error.approval = {
                "summary": "Delete a project file",
                "operation": "delete",
                "resource": "notes.txt",
                "workspace": "/tmp/corax-workspace",
                "network": {"domains": [], "scope": "none"},
                "credentials": ["workspace access"],
                "risk": "high",
            }
            error.choices = ("allow_once", "allow_session", "deny_once")
            raise error
        if cap_id == "gateway":
            self.gateway_calls.append(payload)
            if op == "prepare_turn":
                text = payload.get("text", "")
                return {
                    "operation": "prepare_turn",
                    "ok": True,
                    "session_id": "gw-session",
                    "allow_outbound_file": "пришли" in text.lower() or "send" in text.lower(),
                    "user_message": {"role": "user", "content": f"[GW] {text}"},
                    "messages": [
                        {"role": "system", "content": "gateway system"},
                        {"role": "user", "content": f"[GW] {text}"},
                    ],
                    "recent_files": [],
                }
            if op == "plan_file_delivery":
                return {
                    "operation": "plan_file_delivery",
                    "ok": True,
                    "delivery": {
                        "allowed": True,
                        "path": "/tmp/corax-workspace/gateway-planned.txt",
                        "caption": payload.get("caption"),
                    },
                }
            if op == "plan_tool_recovery":
                result = dict(payload.get("tool_result") or {})
                failed = result.get("ok") is False or any(key in result for key in ("error", "errors", "exception"))
                recovery_prompt = ""
                if failed:
                    result.setdefault("ok", False)
                    result["error_kind"] = "missing_file_or_resource"
                    result["recovery_steps"] = ["list or search nearby paths"]
                    result["recovery_hint"] = "try another available tool before asking the user"
                    recovery_prompt = "A tool failed and no recovery attempt has been made yet."
                return {
                    "operation": "plan_tool_recovery",
                    "ok": True,
                    "tool_failed": failed,
                    "tool_result": result,
                    "error_kind": result.get("error_kind", ""),
                    "recovery_steps": result.get("recovery_steps", []),
                    "recovery_prompt": recovery_prompt,
                }
            if op == "new_session":
                return {"operation": op, "ok": True, "session_id": "gw-new-session"}
            if op in {"record_turn", "record_artifact", "forget_session"}:
                return {"operation": op, "ok": True}
            return {"operation": op, "ok": True}
        if op == "poll":
            return {"updates": self.poll_batches.pop(0) if self.poll_batches else [], "next_offset": 999}
        if op == "chat_action":
            return {"ok": True}
        if op == "send":
            self.sends.append(payload["text"])
            return {"message_id": 1}
        if op == "send_document":
            self.documents.append(payload)
            return {"message_id": 2}
        if op == "set_bot_commands":
            return {"ok": True, "commands": payload.get("commands", [])}
        if op == "stream":  # progressive reveal of the final answer
            if payload.get("done"):
                self.sends.append(payload["text"])
            return {
                "message_id": payload.get("message_id") or 1,
                "edited": True,
                "sent_text": payload.get("text", ""),
            }
        if cap_id == "llm.local" and op == "generate":
            resp = self.llm_responses.pop(0) if self.llm_responses else {"text": "(default)"}
            tcs = resp.get("tool_calls") or []
            return {"text": resp.get("text", ""), "tool_calls": tcs,
                    "finish_reason": resp.get("finish_reason")
                    or ("tool_calls" if tcs else "stop")}
        self.tools_run.append((cap_id, payload))  # a tool invocation
        return self.tool_results.get(cap_id, {"ok": True})


def _tool_call(name, arguments="{}", id="c1"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _text_update(chat_id, text, *, chat_type=None):
    update = {"chat_id": chat_id, "text": text, "command": {"is_command": False}}
    if chat_type is not None:
        update["chat_type"] = chat_type
    return update


def _cmd_update(chat_id, command, args="", reply=None):
    return {"chat_id": chat_id, "text": f"/{command}",
            "command": {"is_command": True, "command": command, "args": args, "reply": reply}}


def _last_tool_message(messages):
    return next(message for message in reversed(messages) if message.get("role") == "tool")


async def _nosleep(_seconds):
    return None


def _async_return(value):
    """Build an async tool_router that always returns ``value``."""
    async def _router(_query, _specs):
        return value

    return _router


def _gateway(backend, **kwargs):
    kwargs.setdefault("capabilities", CAPS)
    kwargs.setdefault("sleep", _nosleep)
    kwargs.setdefault("new_session", lambda: "sess-fixed")
    return CoraxTelegramGateway(
        run_capability=backend.run_capability,
        **kwargs,
    )


def _streamer(events):
    async def stream_capability(_cap_id, _payload, *, session_id=None):
        for event in events:
            yield event

    return stream_capability


def _streamer_batches(batches):
    batches = list(batches)

    async def stream_capability(_cap_id, _payload, *, session_id=None):
        events = batches.pop(0) if batches else [{"type": "done", "finish_reason": "stop", "tool_calls": []}]
        for event in events:
            yield event

    return stream_capability


class ToolSpecTests(unittest.IsolatedAsyncioTestCase):
    def test_tools_built_from_capabilities_excluding_infra(self) -> None:
        gw = _gateway(FakeBackend())
        names = {t["function"]["name"] for t in gw._tool_specs}
        self.assertEqual(names, {"filesystem", "shell", "clock"})
        self.assertEqual(gw._tool_to_cap["filesystem"], "filesystem")
        clock = next(t for t in gw._tool_specs if t["function"]["name"] == "clock")
        self.assertEqual(clock["function"]["description"], "clock")  # falls back to id
        self.assertEqual(clock["function"]["parameters"], {"type": "object", "properties": {}})

    def test_dotted_ids_become_safe_tool_names(self) -> None:
        gw = _gateway(FakeBackend(), capabilities=[{"id": "web.search", "input_schema": {}}])
        self.assertIn("web_search", gw._tool_to_cap)
        self.assertEqual(gw._tool_to_cap["web_search"], "web.search")

    def test_gateway_capability_is_internal_not_model_tool(self) -> None:
        gw = _gateway(FakeBackend(), capabilities=CAPS_WITH_GATEWAY)
        names = {t["function"]["name"] for t in gw._tool_specs}
        self.assertNotIn("telegram_send_document", names)
        self.assertNotIn("gateway", names)

    async def test_active_tools_are_selected_per_turn(self) -> None:
        def selector(query, _specs):
            self.assertEqual(query, "read file")
            return ["filesystem"]

        gw = _gateway(FakeBackend(), tool_selector=selector)
        names = {t["function"]["name"] for t in await gw._active_tool_specs("read file", allow_media=False)}
        self.assertEqual(names, {"filesystem"})

    async def test_empty_selection_offers_no_tools(self) -> None:
        # A no-intent turn (selector returns nothing) must carry no tools, not
        # the whole catalogue — dynamic selection exists to keep the prompt small.
        gw = _gateway(FakeBackend(), tool_selector=lambda _query, _specs: [])
        specs = await gw._active_tool_specs("привет", allow_media=False)
        self.assertEqual(specs, [])

    async def test_selector_error_fails_closed(self) -> None:
        def boom(_query, _specs):
            raise RuntimeError("selector down")

        gw = _gateway(FakeBackend(), tool_selector=boom)
        self.assertEqual(
            await gw._active_tool_specs("read file", allow_media=False),
            [],
        )

    async def test_async_tool_router_selects_tools(self) -> None:
        async def router(query, _specs):
            self.assertEqual(query, "delete a file")
            return ["filesystem"]

        gw = _gateway(FakeBackend(), tool_router=router)
        names = {t["function"]["name"] for t in await gw._active_tool_specs("delete a file", allow_media=False)}
        self.assertEqual(names, {"filesystem"})

    async def test_async_tool_router_empty_offers_no_tools(self) -> None:
        gw = _gateway(FakeBackend(), tool_router=_async_return([]))
        self.assertEqual(await gw._active_tool_specs("привет", allow_media=False), [])

    async def test_async_tool_router_error_fails_closed(self) -> None:
        async def boom(_query, _specs):
            raise RuntimeError("router down")

        gw = _gateway(FakeBackend(), tool_router=boom)
        self.assertEqual(
            await gw._active_tool_specs("read file", allow_media=False),
            [],
        )

    async def test_send_document_is_never_model_callable(self) -> None:
        gw = _gateway(FakeBackend(), tool_selector=lambda _query, _specs: ["filesystem"])
        without_media = {t["function"]["name"] for t in await gw._active_tool_specs("create file", allow_media=False)}
        with_media = {t["function"]["name"] for t in await gw._active_tool_specs("send file", allow_media=True)}
        self.assertNotIn("telegram_send_document", without_media)
        self.assertNotIn("telegram_send_document", with_media)

    def test_shell_tool_log_args_are_compact(self) -> None:
        gw = _gateway(FakeBackend())
        command = "python3 -c \"" + "\n".join(f"print({i})" for i in range(30)) + "\""
        rendered = gw._format_tool_args_for_log("shell", {"command": command})
        self.assertIn('command="python3 -c', rendered)
        self.assertNotIn("\n", rendered)
        self.assertLess(len(rendered), 170)

    def test_filesystem_tool_log_args_show_operation_and_path(self) -> None:
        gw = _gateway(FakeBackend())
        rendered = gw._format_tool_args_for_log(
            "filesystem",
            {"operation": "write", "path": "notes.txt", "content": "secret long content"},
        )
        self.assertEqual(rendered, 'operation=write path="notes.txt"')

    def test_tool_activity_label_omits_secrets_and_raw_shell_command(self) -> None:
        gw = _gateway(FakeBackend())
        generic = gw._tool_activity_label(
            "web.search",
            {"query": "weather", "api_key": "top-secret"},
        )
        shell = gw._tool_activity_label(
            "shell",
            {"command": "API_KEY=top-secret curl https://example.com/private"},
        )
        self.assertIn('query="weather"', generic)
        self.assertNotIn("top-secret", generic)
        self.assertNotIn("example.com", shell)

    def test_failed_tool_result_has_minimal_fallback_without_gateway(self) -> None:
        gw = _gateway(FakeBackend())
        gw._has_gateway_capability = False
        plan = asyncio.run(
            gw._plan_tool_recovery(
                "session",
                "filesystem",
                {},
                {"error": "missing module"},
            )
        )
        self.assertTrue(plan["tool_failed"])
        self.assertFalse(plan["tool_result"]["ok"])
        self.assertIn("try another available tool", plan["tool_result"]["recovery_hint"])
        self.assertNotIn("error_kind", plan["tool_result"])
        self.assertNotIn("recovery_steps", plan["tool_result"])


class ChatToolLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_is_not_treated_as_recoverable_tool_failure(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "delete file")]],
            llm_responses=[
                {
                    "tool_calls": [
                        _tool_call(
                            "filesystem",
                            '{"operation": "delete", "path": "notes.txt"}',
                        )
                    ]
                },
                {"text": "Please approve the pending action."},
            ],
        )
        backend.confirm_capability = "filesystem"
        await _gateway(backend).run(max_iterations=1)
        generate_calls = [
            payload
            for cap_id, operation, payload in backend.calls
            if cap_id == "llm.local" and operation == "generate"
        ]
        self.assertEqual(len(generate_calls), 2)
        tool_message = _last_tool_message(generate_calls[1]["messages"])
        self.assertIn("/security approve task-confirm-1", tool_message["content"])
        self.assertTrue(
            any("Please approve" in message for message in backend.sends)
        )
        self.assertTrue(any("🔧 TOOL · filesystem" in message for message in backend.sends))
        approval = next(message for message in backend.sends if "⏸ APPROVAL" in message)
        self.assertIn("Action: Delete a project file", approval)
        self.assertIn("Resource: notes.txt", approval)
        self.assertIn("Risk: high", approval)
        self.assertIn("/security approve task-confirm-1 once", approval)
        self.assertIn("/security approve task-confirm-1 session", approval)
        self.assertIn("/security deny task-confirm-1 once", approval)
        self.assertNotIn(" turn", approval)
        self.assertNotIn(" rule", approval)

    async def test_tool_call_keeps_session_and_trusted_policy_context(self) -> None:
        backend = FakeBackend(
            poll_batches=[[
                {
                    **_text_update(5, "list files"),
                    "update_id": 123,
                }
            ]],
            llm_responses=[
                {"tool_calls": [_tool_call("filesystem", '{"path": "."}')]},
                {"text": "done"},
            ],
        )
        gw = _gateway(backend, workspace_path="/tmp/corax-workspace")
        await gw.run(max_iterations=1)
        self.assertEqual(len(backend.policy_calls), 1)
        capability, session_id, metadata = backend.policy_calls[0]
        self.assertEqual(capability, "filesystem")
        self.assertEqual(session_id, "sess-fixed")
        self.assertEqual(
            metadata,
            {
                "actor": "5",
                "transport": "telegram",
                "workspace": "/tmp/corax-workspace",
                "environment": "telegram",
                "turn_id": "telegram:5:123",
            },
        )

    async def test_meta_tool_call_unwraps_for_policy_and_artifacts(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "write file")]],
            llm_responses=[
                {
                    "tool_calls": [
                        _tool_call(
                            "tool_call",
                            json.dumps(
                                {
                                    "capability": "filesystem",
                                    "input": {
                                        "operation": "write",
                                        "path": "note.txt",
                                        "content": "hello",
                                    },
                                }
                            ),
                        )
                    ]
                },
                {"text": "done"},
            ],
        )
        backend.tool_results["filesystem"] = {
            "ok": True,
            "path": "note.txt",
            "written": True,
        }

        await _gateway(
            backend,
            capabilities=[
                *CAPS,
                {"id": "tool.call", "model_name": "tool_call"},
            ],
        ).run(max_iterations=1)

        self.assertEqual(backend.policy_calls[0][0], "filesystem")
        self.assertTrue(any("filesystem" in message for message in backend.sends))

    async def test_plain_answer_no_tools(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]],
                              llm_responses=[{"text": "hello there"}])
        gw = _gateway(backend, model="gemma-4")
        await gw.run(max_iterations=1)
        self.assertIn("hello there", backend.sends)
        gen = next(p for c, op, p in backend.calls if op == "generate")
        self.assertEqual(gen["model"], "gemma-4")
        self.assertTrue(gen["tools"])  # tools were offered
        self.assertEqual(gen["messages"][0]["role"], "system")
        self.assertEqual(gen["messages"][1]["role"], "user")
        self.assertRegex(gen["messages"][1]["content"], r"^\[[A-Z][a-z]{2} \d{4}-\d{2}-\d{2} ")

    async def test_batch_output_limit_is_not_recorded_as_an_answer(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "hi")]],
            llm_responses=[
                {"text": "internal draft", "finish_reason": "length"}
            ],
        )

        await _gateway(backend).run(max_iterations=1)

        self.assertNotIn("internal draft", backend.sends)
        self.assertTrue(
            any("partial response was discarded" in text for text in backend.sends)
        )
        self.assertNotIn(
            "record_turn",
            [operation for _capability, operation, _payload in backend.calls],
        )

    async def test_streaming_answer_is_sent_live_without_duplicate_final(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi", chat_type="private")]])
        gw = _gateway(
            backend,
            stream_capability=_streamer([
                {"type": "delta", "content": "hello "},
                {"type": "delta", "content": "there"},
                {"type": "done", "finish_reason": "stop", "tool_calls": []},
            ]),
        )
        await gw.run(max_iterations=1)
        stream_calls = [p for _c, op, p in backend.calls if op == "stream"]
        generate_calls = [p for _c, op, p in backend.calls if op == "generate"]
        self.assertEqual(generate_calls, [])
        self.assertTrue(stream_calls[-1]["done"])
        self.assertEqual(stream_calls[-1]["text"], "hello there")
        self.assertEqual(stream_calls[0]["transport"], "edit")
        self.assertEqual(stream_calls[0]["chat_type"], "private")
        self.assertIsInstance(stream_calls[0]["draft_id"], int)
        self.assertEqual(stream_calls[-1]["draft_id"], stream_calls[0]["draft_id"])
        self.assertEqual(backend.sends.count("hello there"), 1)

    async def test_streaming_output_limit_discards_partial_answer(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]])
        gw = _gateway(
            backend,
            stream_capability=_streamer(
                [
                    {"type": "reasoning", "content": "unfinished thought"},
                    {"type": "delta", "content": "internal draft"},
                    {
                        "type": "done",
                        "finish_reason": "length",
                        "tool_calls": [],
                    },
                ]
            ),
        )

        await gw.run(max_iterations=1)

        stream_calls = [p for _c, op, p in backend.calls if op == "stream"]
        self.assertTrue(stream_calls[-1]["done"])
        self.assertIn("partial response was discarded", stream_calls[-1]["text"])
        self.assertNotIn(
            "record_turn",
            [operation for _capability, operation, _payload in backend.calls],
        )

    async def test_streaming_strips_model_channel_marker(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]])
        gw = _gateway(
            backend,
            stream_capability=_streamer([
                {"type": "delta", "content": "<|channel>\n"},
                {"type": "delta", "content": "hello"},
                {"type": "done", "finish_reason": "stop", "tool_calls": []},
            ]),
        )
        await gw.run(max_iterations=1)
        stream_calls = [p for _c, op, p in backend.calls if op == "stream"]
        self.assertTrue(stream_calls[-1]["done"])
        self.assertEqual(stream_calls[-1]["text"], "hello")
        self.assertNotIn("<|channel>", backend.sends)
        self.assertIn("hello", backend.sends)

    async def test_streaming_strips_model_thought_markers(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]])
        gw = _gateway(
            backend,
            stream_capability=_streamer([
                {"type": "delta", "content": "thought\n"},
                {"type": "delta", "content": "thought▌\n"},
                {"type": "delta", "content": "<thought>\n"},
                {"type": "delta", "content": "Ответ готов."},
                {"type": "done", "finish_reason": "stop", "tool_calls": []},
            ]),
        )
        await gw.run(max_iterations=1)
        stream_calls = [p for _c, op, p in backend.calls if op == "stream"]
        self.assertTrue(stream_calls[-1]["done"])
        self.assertEqual(stream_calls[-1]["text"], "Ответ готов.")
        self.assertNotIn("thought", "\n".join(backend.sends).lower())

    async def test_streaming_drops_only_thought_markers(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]])
        gw = _gateway(
            backend,
            stream_capability=_streamer([
                {"type": "delta", "content": "thought\n"},
                {"type": "delta", "content": "thought▌\n"},
                {"type": "done", "finish_reason": "stop", "tool_calls": []},
            ]),
        )
        await gw.run(max_iterations=1)
        self.assertEqual([p for _c, op, p in backend.calls if op == "stream"], [])
        self.assertIn("(no response)", backend.sends)

    async def test_stream_transport_can_be_overridden(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi", chat_type="private")]])
        gw = _gateway(
            backend,
            stream_capability=_streamer([
                {"type": "delta", "content": "hello"},
                {"type": "done", "finish_reason": "stop", "tool_calls": []},
            ]),
            stream_transport="auto",
        )
        await gw.run(max_iterations=1)
        stream_call = next(p for _c, op, p in backend.calls if op == "stream")
        self.assertEqual(stream_call["transport"], "auto")

    async def test_streaming_buffers_small_deltas_before_telegram_flush(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]])
        gw = _gateway(
            backend,
            stream_capability=_streamer(
                [{"type": "delta", "content": ch} for ch in "abcdefghij"]
                + [{"type": "done", "finish_reason": "stop", "tool_calls": []}]
            ),
            reveal_buffer_threshold=100,
            reveal_edit_interval_ms=10_000,
        )
        await gw.run(max_iterations=1)
        stream_calls = [p for _c, op, p in backend.calls if op == "stream"]
        self.assertEqual(len(stream_calls), 2)
        self.assertFalse(stream_calls[0]["done"])
        self.assertEqual(stream_calls[0]["text"], "a")
        self.assertTrue(stream_calls[1]["done"])
        self.assertEqual(stream_calls[1]["text"], "abcdefghij")

    async def test_streaming_tool_call_is_executed_by_gateway_loop(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "list files")]],
            llm_responses=[{"text": "done"}],
            tool_results={"filesystem": {"files": ["a.txt"]}},
        )
        gw = _gateway(
            backend,
            stream_capability=_streamer_batches([
                [
                    {
                        "type": "done",
                        "finish_reason": "tool_calls",
                        "tool_calls": [_tool_call("filesystem", '{"path": "."}')],
                    }
                ],
                [
                    {"type": "delta", "content": "done"},
                    {"type": "done", "finish_reason": "stop", "tool_calls": []},
                ],
            ]),
        )
        await gw.run(max_iterations=1)
        self.assertEqual(backend.tools_run[0][0], "filesystem")
        self.assertEqual(backend.tools_run[0][1], {"path": "."})
        self.assertIn("done", backend.sends)

    async def test_generate_receives_only_active_selected_tools(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "исправь файл")]],
            llm_responses=[{"text": "ok"}],
        )
        gw = _gateway(
            backend,
            capabilities=[
                *CAPS,
                {"id": "editor", "description": "editor", "input_schema": {"type": "object", "properties": {}}},
            ],
            tool_selector=lambda _query, _specs: ["editor", "filesystem"],
        )
        await gw.run(max_iterations=1)
        gen = next(p for _c, op, p in backend.calls if op == "generate")
        names = {tool["function"]["name"] for tool in gen["tools"]}
        self.assertEqual(names, {"editor", "filesystem"})

    async def test_gateway_capability_prepares_and_records_turn(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "hi")]],
            llm_responses=[{"text": "hello from llm"}],
        )
        gw = _gateway(backend, capabilities=CAPS_WITH_GATEWAY)
        await gw.run(max_iterations=1)
        gen = next(p for _c, op, p in backend.calls if op == "generate")
        self.assertEqual(gen["messages"][0]["content"], "gateway system")
        self.assertEqual(gen["messages"][1]["content"], "[GW] hi")
        ops = [call["operation"] for call in backend.gateway_calls]
        self.assertEqual(ops, ["prepare_turn", "record_turn"])

    async def test_media_tag_without_user_request_does_not_send_document(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "create test.txt")]],
            llm_responses=[
                {"tool_calls": [_tool_call("filesystem", '{"operation": "write", "path": "test.txt", "content": "hi"}')]},
                {"text": "Готово.\nMEDIA:test.txt"},
            ],
            tool_results={"filesystem": {"path": "test.txt", "written": True, "size": 2}},
        )
        gw = _gateway(backend, workspace_path="/tmp/corax-workspace")
        await gw.run(max_iterations=1)
        self.assertIn("Готово.", backend.sends)
        self.assertNotIn("MEDIA:test.txt", backend.sends)
        self.assertEqual(backend.documents, [])

    async def test_media_tag_with_user_request_sends_document(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "create and send test.txt")]],
            llm_responses=[
                {"tool_calls": [_tool_call("filesystem", '{"operation": "write", "path": "test.txt", "content": "hi"}')]},
                {"text": "Готово.\nMEDIA:test.txt"},
            ],
            tool_results={"filesystem": {"path": "test.txt", "written": True, "size": 2}},
        )
        gw = _gateway(backend, workspace_path="/tmp/corax-workspace")
        await gw.run(max_iterations=1)
        self.assertIn("Готово.", backend.sends)
        self.assertNotIn("MEDIA:test.txt", backend.sends)
        self.assertEqual(backend.documents[0]["path"], "/tmp/corax-workspace/test.txt")
        self.assertIn("test.txt", backend.documents[0]["caption"])

    async def test_hallucinated_send_document_tool_is_denied(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "create test.txt")]],
            llm_responses=[
                {"tool_calls": [_tool_call("telegram_send_document", '{"path": "test.txt"}')]},
                {"text": "Файл создан."},
            ],
        )
        gw = _gateway(backend, workspace_path="/tmp/corax-workspace")
        await gw.run(max_iterations=1)
        self.assertEqual(backend.documents, [])
        gen_calls = [p for _c, op, p in backend.calls if op == "generate"]
        tool_msg = _last_tool_message(gen_calls[1]["messages"])
        self.assertEqual(tool_msg["role"], "tool")
        self.assertIn("host-controlled", tool_msg["content"])

    async def test_send_document_pseudo_tool_cannot_bypass_kernel(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "пришли test.txt")]],
            llm_responses=[
                {"tool_calls": [_tool_call("telegram_send_document", '{"path": "test.txt", "caption": "готово"}')]},
                {"text": "Отправил."},
            ],
        )
        gw = _gateway(backend, workspace_path="/tmp/corax-workspace")
        await gw.run(max_iterations=1)
        self.assertEqual(backend.documents, [])
        self.assertTrue(
            any("telegram_send_document" in message and "failed" in message for message in backend.sends)
        )

    async def test_explicit_file_request_auto_sends_created_file(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "найди новости, запиши в файл и отправь мне сюда")]],
            llm_responses=[
                {
                    "tool_calls": [
                        _tool_call(
                            "filesystem",
                            '{"operation": "write", "path": "ukraine_news.txt", "content": "news"}',
                        )
                    ]
                },
                {"text": "Файл сохранен локально и готов предоставить."},
            ],
            tool_results={"filesystem": {"path": "ukraine_news.txt", "written": True, "size": 4}},
        )
        gw = _gateway(backend, workspace_path="/tmp/corax-workspace")
        await gw.run(max_iterations=1)
        self.assertEqual(len(backend.documents), 1)
        self.assertEqual(backend.documents[0]["path"], "/tmp/corax-workspace/ukraine_news.txt")
        self.assertTrue(any("Файл сохранен локально" in sent for sent in backend.sends))

    async def test_gateway_does_not_plan_direct_model_document_delivery(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "пришли test.txt")]],
            llm_responses=[
                {"tool_calls": [_tool_call("telegram_send_document", '{"path": "test.txt"}')]},
                {"text": "Отправил."},
            ],
        )
        gw = _gateway(backend, capabilities=CAPS_WITH_GATEWAY, workspace_path="/tmp/corax-workspace")
        await gw.run(max_iterations=1)
        self.assertEqual(backend.documents, [])
        ops = [call["operation"] for call in backend.gateway_calls]
        self.assertNotIn("plan_file_delivery", ops)

    async def test_recent_created_file_is_available_on_next_turn(self) -> None:
        backend = FakeBackend(
            poll_batches=[
                [_text_update(5, "создай Test2.txt")],
                [_text_update(5, "пришли мне файл который я просил тебя создать")],
            ],
            llm_responses=[
                {"tool_calls": [_tool_call("filesystem", '{"operation": "write", "path": "Test2.txt", "content": "hi"}')]},
                {"text": "Файл создан."},
                {"tool_calls": [_tool_call("telegram_send_document", '{"path": "Test2.txt"}')]},
                {"text": "Отправил."},
            ],
            tool_results={"filesystem": {"path": "Test2.txt", "written": True, "size": 2}},
        )
        gw = _gateway(backend, workspace_path="/tmp/corax-workspace")
        await gw.run(max_iterations=2)
        gen_calls = [p for _c, op, p in backend.calls if op == "generate"]
        second_turn_messages = gen_calls[2]["messages"]
        self.assertTrue(
            any("Recent local files" in m["content"] and "Test2.txt" in m["content"] for m in second_turn_messages)
        )
        self.assertEqual(backend.documents, [])
        self.assertTrue(
            any("telegram_send_document" in message and "failed" in message for message in backend.sends)
        )

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
        self.assertTrue(
            any(
                message == '🔧 TOOL · filesystem · path="."'
                for message in backend.sends
            )
        )
        self.assertTrue(
            any(
                message == '✅ TOOL · filesystem · path="." · completed'
                for message in backend.sends
            )
        )

    async def test_tool_activity_never_exposes_args_or_results(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "write notes")]],
            llm_responses=[
                {
                    "tool_calls": [
                        _tool_call(
                            "filesystem",
                            '{"operation": "write", "path": "notes.txt", '
                            '"content": "argument-secret"}',
                        )
                    ]
                },
                {"text": "done"},
            ],
            tool_results={
                "filesystem": {
                    "path": "notes.txt",
                    "written": True,
                    "private": "result-secret",
                }
            },
        )
        await _gateway(backend).run(max_iterations=1)
        visible = "\n".join(backend.sends)
        self.assertIn('operation=write path="notes.txt"', visible)
        self.assertNotIn("argument-secret", visible)
        self.assertNotIn("result-secret", visible)

    async def test_gateway_capability_records_tool_artifact(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "create notes")]],
            llm_responses=[
                {"tool_calls": [_tool_call("filesystem", '{"operation": "write", "path": "notes.txt", "content": "hi"}')]},
                {"text": "done"},
            ],
            tool_results={"filesystem": {"path": "notes.txt", "written": True, "size": 2}},
        )
        gw = _gateway(backend, capabilities=CAPS_WITH_GATEWAY)
        await gw.run(max_iterations=1)
        artifact_calls = [
            call for call in backend.gateway_calls if call["operation"] == "record_artifact"
        ]
        self.assertEqual(artifact_calls[0]["tool_capability_id"], "filesystem")
        self.assertEqual(artifact_calls[0]["tool_result"]["path"], "notes.txt")

    async def test_failed_write_is_not_recorded_as_recent_artifact(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "create notes")]],
            llm_responses=[
                {
                    "tool_calls": [
                        _tool_call(
                            "filesystem",
                            '{"operation": "write", "path": "failed.txt"}',
                        )
                    ]
                },
                {"text": "could not write"},
                {"text": "stopped"},
            ],
            tool_results={
                "filesystem": {
                    "ok": False,
                    "error": "write failed",
                    "path": "failed.txt",
                }
            },
        )
        gateway = _gateway(backend, capabilities=CAPS_WITH_GATEWAY)

        await gateway.run(max_iterations=1)

        artifact_calls = [
            call
            for call in backend.gateway_calls
            if call["operation"] == "record_artifact"
        ]
        self.assertEqual(artifact_calls, [])
        self.assertFalse(
            any(
                "failed.txt" in files
                for files in gateway._recent_files.values()
            )
        )

    async def test_gateway_capability_plans_tool_recovery(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "read notes")]],
            llm_responses=[
                {"tool_calls": [_tool_call("filesystem", '{"operation": "read", "path": "missing.txt"}')]},
                {"text": "ok"},
            ],
        )
        backend.fail_capability = "filesystem"
        await _gateway(backend, capabilities=CAPS_WITH_GATEWAY).run(max_iterations=1)
        recovery_calls = [
            call for call in backend.gateway_calls if call["operation"] == "plan_tool_recovery"
        ]
        self.assertEqual(recovery_calls[0]["tool_capability_id"], "filesystem")
        self.assertEqual(recovery_calls[0]["tool_result"]["error"], "boom")

    async def test_same_chat_reuses_session_history(self) -> None:
        backend = FakeBackend(
            poll_batches=[
                [_text_update(5, "my name is Alex")],
                [_text_update(5, "what is my name?")],
            ],
            llm_responses=[{"text": "Nice to meet you, Alex."}, {"text": "Alex"}],
        )
        gw = _gateway(backend)
        await gw.run(max_iterations=2)
        gen_calls = [p for _c, op, p in backend.calls if op == "generate"]
        second_messages = gen_calls[1]["messages"]
        self.assertTrue(
            any(m["role"] == "assistant" and "Nice to meet you" in m["content"] for m in second_messages)
        )
        self.assertTrue(
            any(m["role"] == "user" and "my name is Alex" in m["content"] for m in second_messages)
        )

    async def test_fallback_state_survives_gateway_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "telegram-gateway-state.json"
            first_backend = FakeBackend(
                poll_batches=[[_text_update(5, "найди новости экономики")]],
                llm_responses=[{"text": "Ищу свежие новости экономики."}],
            )
            await _gateway(first_backend, state_path=state_path).run(max_iterations=1)

            second_backend = FakeBackend(
                poll_batches=[[_text_update(5, "а теперь по России")]],
                llm_responses=[{"text": "Продолжаю по России."}],
            )
            await _gateway(second_backend, state_path=state_path).run(max_iterations=1)

        gen_calls = [p for _c, op, p in second_backend.calls if op == "generate"]
        messages = gen_calls[0]["messages"]
        self.assertTrue(any("новости экономики" in m.get("content", "") for m in messages))
        self.assertTrue(any("Ищу свежие новости экономики" in m.get("content", "") for m in messages))

    async def test_local_standby_state_survives_failed_gateway_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "telegram-gateway-state.json"
            session_ids = iter(["local-1", "local-2"])
            first_backend = FakeBackend(
                poll_batches=[[_text_update(5, "remember this")]],
                llm_responses=[{"text": "remembered locally"}],
            )
            first_backend.fail_capability = "gateway"
            first = _gateway(
                first_backend,
                capabilities=CAPS_WITH_GATEWAY,
                state_path=state_path,
                new_session=lambda: next(session_ids),
            )
            await first.run(max_iterations=1)
            self.assertTrue(state_path.exists())
            self.assertEqual(first._sessions["5"], "local-1")

            second_backend = FakeBackend(
                poll_batches=[[_text_update(5, "what did I say?")]],
                llm_responses=[{"text": "you asked me to remember this"}],
            )
            second_backend.fail_capability = "gateway"
            second = _gateway(
                second_backend,
                capabilities=CAPS_WITH_GATEWAY,
                state_path=state_path,
                new_session=lambda: next(session_ids),
            )
            await second.run(max_iterations=1)

        self.assertEqual(second._sessions["5"], "local-1")
        generate_calls = [
            payload
            for capability_id, operation, payload in second_backend.calls
            if capability_id == "llm.local" and operation == "generate"
        ]
        messages = generate_calls[0]["messages"]
        self.assertTrue(
            any("remember this" in message.get("content", "") for message in messages)
        )
        self.assertTrue(
            any(
                "remembered locally" in message.get("content", "")
                for message in messages
            )
        )

    async def test_profile_memory_survives_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_path = Path(tmpdir) / "profile.md"
            backend = FakeBackend(
                poll_batches=[
                    [_text_update(5, "Меня зовут Алекс, пиши со мной на русском.")],
                    [_cmd_update(5, "new_session")],
                    [_text_update(5, "как меня зовут?")],
                ],
                llm_responses=[{"text": "Запомнил имя."}, {"text": "Алекс."}],
            )
            sessions = iter(["sess-1", "sess-2"])
            await _gateway(
                backend,
                profile_path=profile_path,
                new_session=lambda: next(sessions),
            ).run(max_iterations=3)

            profile_text = profile_path.read_text(encoding="utf-8")

        self.assertIn("Меня зовут Алекс", profile_text)
        gen_calls = [p for _c, op, p in backend.calls if op == "generate"]
        messages = gen_calls[1]["messages"]
        system_text = messages[0]["content"]
        self.assertIn("Remembered User Profile", system_text)
        self.assertIn("Onboarding status: completed", system_text)
        self.assertIn("Do not repeat first-contact", system_text)
        self.assertIn("Меня зовут Алекс", system_text)
        self.assertFalse(any(m.get("role") == "assistant" and "Запомнил имя" in m.get("content", "") for m in messages))

    async def test_profile_memory_skips_secret_like_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_path = Path(tmpdir) / "profile.md"
            backend = FakeBackend(
                poll_batches=[[_text_update(5, "запомни токен abc123")]],
                llm_responses=[{"text": "Не сохраняю секреты."}],
            )
            await _gateway(backend, profile_path=profile_path).run(max_iterations=1)

            self.assertFalse(profile_path.exists())

    async def test_profile_memory_saves_answer_to_onboarding_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_path = Path(tmpdir) / "profile.md"
            backend = FakeBackend(
                poll_batches=[
                    [_text_update(5, "привет")],
                    [_text_update(5, "Алекс, русский, проект Corax")],
                ],
                llm_responses=[
                    {"text": "Как вас зовут, какой язык предпочитаете и с какими проектами помогать?"},
                    {"text": "Отлично."},
                ],
            )
            await _gateway(backend, profile_path=profile_path).run(max_iterations=2)

            profile_text = profile_path.read_text(encoding="utf-8")

        self.assertIn("Алекс, русский, проект Corax", profile_text)

    async def test_memory_loop_context_is_injected_before_user_message(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "как меня зовут?")]],
            llm_responses=[{"text": "Алекс."}],
        )
        recalls = []

        async def before(text, *, session_id, scope):
            recalls.append((text, session_id, scope))
            return {
                "context": (
                    '<memory_context trust="untrusted-data">\n'
                    "- Меня зовут Алекс\n"
                    "</memory_context>"
                )
            }

        gateway = _gateway(backend, memory_before_turn=before)
        await gateway.run(max_iterations=1)

        messages = [
            payload
            for _cap, operation, payload in backend.calls
            if operation == "generate"
        ][0]["messages"]
        memory_index = next(
            index
            for index, message in enumerate(messages)
            if "memory_context" in str(message.get("content"))
        )
        user_index = next(
            index
            for index, message in enumerate(messages)
            if message.get("role") == "user"
        )
        self.assertLess(memory_index, user_index)
        self.assertEqual(recalls[0][2]["channel"], "telegram")

    async def test_host_prompt_assembly_keeps_raw_telegram_messages(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "hello")]],
            llm_responses=[{"text": "hi"}],
        )

        async def before(*_args, **_kwargs):
            raise AssertionError("host prompt assembly owns recall")

        await _gateway(
            backend,
            host_prompt_assembly=True,
            memory_before_turn=before,
        ).run(max_iterations=1)

        messages = [
            payload
            for _cap, operation, payload in backend.calls
            if operation == "generate"
        ][0]["messages"]
        self.assertEqual(messages, [{"role": "user", "content": "hello"}])

    async def test_failed_host_turn_is_aborted(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "hello")]],
        )
        backend.fail_operation = "generate"
        aborted = []

        async def abort(*, session_id, scope):
            aborted.append((session_id, scope))
            return {"aborted": True}

        await _gateway(
            backend,
            capabilities=CAPS_WITH_GATEWAY,
            host_prompt_assembly=True,
            abort_turn=abort,
        ).run(max_iterations=1)

        self.assertEqual(len(aborted), 1)
        self.assertEqual(aborted[0][0], "gw-session")
        self.assertEqual(aborted[0][1]["channel"], "telegram")
        self.assertTrue(aborted[0][1]["turn_id"].startswith("telegram:"))

    async def test_cancelled_first_generate_aborts_and_reraises(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "hello")]],
        )
        original = backend.run_capability

        async def cancel_generate(
            cap_id,
            payload,
            *,
            session_id=None,
            policy_metadata=None,
        ):
            if cap_id == "llm.local" and payload.get("operation") == "generate":
                raise asyncio.CancelledError
            return await original(
                cap_id,
                payload,
                session_id=session_id,
                policy_metadata=policy_metadata,
            )

        backend.run_capability = cancel_generate
        aborted = []

        async def abort(*, session_id, scope):
            aborted.append((session_id, scope))

        gateway = _gateway(backend, abort_turn=abort)

        with self.assertRaises(asyncio.CancelledError):
            await gateway.run(max_iterations=1)

        self.assertEqual(len(aborted), 1)
        self.assertEqual(aborted[0][1]["channel"], "telegram")

    async def test_cancel_after_confirmation_denies_then_aborts_and_reraises(
        self,
    ) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "delete file")]],
            llm_responses=[
                {"tool_calls": [_tool_call("filesystem", '{"path": "a.txt"}')]}
            ],
        )
        backend.confirm_capability = "filesystem"
        original = backend.run_capability
        model_calls = 0

        async def cancel_second_model(
            cap_id,
            payload,
            *,
            session_id=None,
            policy_metadata=None,
        ):
            nonlocal model_calls
            if cap_id == "llm.local" and payload.get("operation") == "generate":
                model_calls += 1
                if model_calls == 2:
                    raise asyncio.CancelledError
            return await original(
                cap_id,
                payload,
                session_id=session_id,
                policy_metadata=policy_metadata,
            )

        backend.run_capability = cancel_second_model
        cleanup = []

        async def security(command, *, actor, transport):
            cleanup.append(("deny", command))
            return {"ok": True}

        async def abort(*, session_id, scope):
            cleanup.append(("abort", scope["turn_id"]))

        gateway = _gateway(
            backend,
            security_command=security,
            abort_turn=abort,
        )

        with self.assertRaises(asyncio.CancelledError):
            await gateway.run(max_iterations=1)

        self.assertEqual([item[0] for item in cleanup], ["deny", "abort"])
        self.assertEqual(gateway._pending_approvals, {})

    async def test_aborted_turn_denies_and_forgets_pending_approval(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "delete file")]],
            llm_responses=[
                {"tool_calls": [_tool_call("filesystem", '{"path": "a.txt"}')]}
            ],
        )
        backend.confirm_capability = "filesystem"
        original = backend.run_capability
        model_calls = 0

        async def fail_second_model(
            cap_id,
            payload,
            *,
            session_id=None,
            policy_metadata=None,
        ):
            nonlocal model_calls
            if cap_id == "llm.local" and payload.get("operation") == "generate":
                model_calls += 1
                if model_calls == 2:
                    raise GatewayError("model failed")
            return await original(
                cap_id,
                payload,
                session_id=session_id,
                policy_metadata=policy_metadata,
            )

        backend.run_capability = fail_second_model
        security_calls = []

        async def security(command, *, actor, transport):
            security_calls.append((command, actor, transport))
            return {"ok": True}

        gateway = _gateway(
            backend,
            security_command=security,
            abort_turn=lambda **_kwargs: asyncio.sleep(0),
        )
        await gateway.run(max_iterations=1)

        self.assertEqual(
            security_calls,
            [("deny task-confirm-1 once", "5", "telegram")],
        )
        self.assertEqual(gateway._pending_approvals, {})

    async def test_failed_pending_denial_remains_retryable(self) -> None:
        async def security(_command, *, actor, transport):
            return {"ok": False}

        gateway = _gateway(FakeBackend(), security_command=security)
        gateway._pending_approvals["5"] = {"task-1": "filesystem"}

        await gateway._discard_pending_approvals(5)

        self.assertEqual(
            gateway._pending_approvals,
            {"5": {"task-1": "filesystem"}},
        )

    async def test_pending_denial_exception_remains_retryable(self) -> None:
        async def security(_command, *, actor, transport):
            raise GatewayError("unavailable")

        gateway = _gateway(FakeBackend(), security_command=security)
        gateway._pending_approvals["5"] = {"task-1": "filesystem"}

        await gateway._discard_pending_approvals(5)

        self.assertEqual(
            gateway._pending_approvals,
            {"5": {"task-1": "filesystem"}},
        )

    async def test_memory_loop_retains_after_completed_turn(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "Запомни, меня зовут Алекс")]],
            llm_responses=[{"text": "Запомнил."}],
        )
        writes = []

        async def after(user_text, assistant_text, *, session_id, scope):
            writes.append((user_text, assistant_text, session_id, scope))
            return {"stored": True}

        await _gateway(backend, memory_after_turn=after).run(max_iterations=1)
        self.assertEqual(writes[0][0], "Запомни, меня зовут Алекс")
        self.assertEqual(writes[0][1], "Запомнил.")
        self.assertEqual(writes[0][3]["channel"], "telegram")

    async def test_new_session_starts_empty_history(self) -> None:
        backend = FakeBackend(
            poll_batches=[
                [_text_update(5, "remember this")],
                [_cmd_update(5, "new_session")],
                [_text_update(5, "what did I say?")],
            ],
            llm_responses=[{"text": "remembered"}, {"text": "I do not know"}],
        )
        sessions = iter(["sess-1", "sess-2"])
        gw = _gateway(backend, new_session=lambda: next(sessions))
        await gw.run(max_iterations=3)
        gen_calls = [p for _c, op, p in backend.calls if op == "generate"]
        second_messages = gen_calls[1]["messages"]
        self.assertFalse(any("remember this" in m.get("content", "") for m in second_messages))

    async def test_unknown_tool_is_fed_back_as_error(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "x")]],
            llm_responses=[{"tool_calls": [_tool_call("nope")]}, {"text": "ok"}],
        )
        gw = _gateway(backend)
        await gw.run(max_iterations=1)
        # second generate call carries a tool message with the error
        gen_calls = [p for c, op, p in backend.calls if op == "generate"]
        tool_msg = _last_tool_message(gen_calls[1]["messages"])
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
        gw = _gateway(backend, host_prompt_assembly=True)
        await gw.run(max_iterations=1)
        gen_calls = [p for c, op, p in backend.calls if op == "generate"]
        self.assertTrue(gen_calls[1]["_corax_tool_failure"])
        tool_msg = _last_tool_message(gen_calls[1]["messages"])
        self.assertIn("error", tool_msg["content"])
        self.assertIn("recovery_hint", tool_msg["content"])
        self.assertTrue(
            any(message == "❌ TOOL · filesystem · failed" for message in backend.sends)
        )

    async def test_tool_failure_can_be_retried_in_next_step(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "read notes")]],
            llm_responses=[
                {"tool_calls": [_tool_call("filesystem", '{"operation": "read", "path": "missing.txt"}', id="bad")]},
                {"tool_calls": [_tool_call("filesystem", '{"operation": "read", "path": "notes.txt"}', id="good")]},
                {"text": "found it"},
            ],
            tool_results={"filesystem": {"content": "hello"}},
        )
        backend.fail_operation = None

        original = backend.run_capability
        seen_bad = False

        async def fail_first_read(
            cap_id,
            payload,
            *,
            session_id=None,
            policy_metadata=None,
        ):
            nonlocal seen_bad
            if cap_id == "filesystem" and payload.get("path") == "missing.txt" and not seen_bad:
                seen_bad = True
                raise GatewayError("file not found")
            return await original(
                cap_id,
                payload,
                session_id=session_id,
                policy_metadata=policy_metadata,
            )

        backend.run_capability = fail_first_read
        await _gateway(backend).run(max_iterations=1)
        self.assertEqual(backend.tools_run[-1][1]["path"], "notes.txt")
        self.assertIn("found it", backend.sends)

    async def test_gateway_forces_recovery_before_accepting_refusal_after_tool_error(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "read notes")]],
            llm_responses=[
                {"tool_calls": [_tool_call("filesystem", '{"operation": "read", "path": "missing.txt"}', id="bad")]},
                {"text": "Не получилось. Выберите другой файл."},
                {"tool_calls": [_tool_call("filesystem", '{"operation": "read", "path": "notes.txt"}', id="good")]},
                {"text": "Нашел файл."},
            ],
            tool_results={"filesystem": {"content": "hello"}},
        )
        original = backend.run_capability
        seen_bad = False

        async def fail_first_read(
            cap_id,
            payload,
            *,
            session_id=None,
            policy_metadata=None,
        ):
            nonlocal seen_bad
            if cap_id == "filesystem" and payload.get("path") == "missing.txt" and not seen_bad:
                seen_bad = True
                raise GatewayError("file not found")
            return await original(
                cap_id,
                payload,
                session_id=session_id,
                policy_metadata=policy_metadata,
            )

        backend.run_capability = fail_first_read
        await _gateway(backend).run(max_iterations=1)
        gen_calls = [p for _c, op, p in backend.calls if op == "generate"]
        self.assertTrue(
            any(
                "recovery service" in (m.get("content") or "")
                for m in gen_calls[2]["messages"]
            )
        )
        self.assertEqual(backend.tools_run[-1][1]["path"], "notes.txt")
        self.assertIn("Нашел файл.", backend.sends)

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

    async def test_final_answer_strips_model_channel_marker(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "hi")]],
            llm_responses=[{"text": "<channel>\nПривет"}],
        )
        gw = _gateway(backend, stream_capability=None)
        await gw.run(max_iterations=1)
        self.assertNotIn("<channel>", backend.sends)
        self.assertIn("Привет", backend.sends)

    async def test_typing_sent_before_generate(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]], llm_responses=[{"text": "ok"}])
        await _gateway(backend).run(max_iterations=1)
        self.assertTrue(any(op == "chat_action" for _c, op, _p in backend.calls))

    async def test_typing_failure_is_ignored(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]], llm_responses=[{"text": "ok"}])
        backend.fail_operation = "chat_action"  # typing fails — turn must still complete
        await _gateway(backend).run(max_iterations=1)
        self.assertIn("ok", backend.sends)

    async def test_long_answer_is_revealed_progressively(self) -> None:
        long_text = "word " * 60  # > reveal_chunk -> progressive edits
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]],
                              llm_responses=[{"text": long_text}])
        await _gateway(backend, reveal_chunk=64).run(max_iterations=1)
        stream_calls = [p for _c, op, p in backend.calls if op == "stream"]
        self.assertGreater(len(stream_calls), 1)          # multiple progressive edits
        self.assertTrue(stream_calls[-1]["done"])          # last one finalizes
        self.assertEqual(stream_calls[-1]["text"], long_text)
        self.assertIn(long_text, backend.sends)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_session(self) -> None:
        backend = FakeBackend(poll_batches=[[_cmd_update(5, "new_session", reply="🆕")]])
        await _gateway(backend).run(max_iterations=1)
        self.assertIn("🆕", backend.sends)

    async def test_new_session_resets_gateway_capability(self) -> None:
        backend = FakeBackend(poll_batches=[[_cmd_update(5, "new_session", reply="🆕")]])
        gw = _gateway(
            backend,
            capabilities=CAPS_WITH_GATEWAY,
            new_session=lambda: self.fail("local session generator must not run"),
        )
        await gw.run(max_iterations=1)
        calls = [call for call in backend.gateway_calls if call["operation"] == "new_session"]
        self.assertEqual(calls[0]["channel"], "telegram")
        self.assertEqual(calls[0]["conversation_id"], 5)
        self.assertNotIn("session_id", calls[0])
        self.assertEqual(gw._sessions["5"], "gw-new-session")
        self.assertIn("🆕", backend.sends)

    async def test_new_session_uses_local_fallback_when_gateway_fails(self) -> None:
        backend = FakeBackend(poll_batches=[[_cmd_update(5, "new_session", reply="🆕")]])
        backend.fail_operation = "new_session"
        gw = _gateway(
            backend,
            capabilities=CAPS_WITH_GATEWAY,
            new_session=lambda: "local-fallback",
        )
        await gw.run(max_iterations=1)
        self.assertEqual(gw._sessions["5"], "local-fallback")
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

    async def test_status_command(self) -> None:
        backend = FakeBackend(poll_batches=[[_cmd_update(5, "status")]])
        await _gateway(backend, model="gemma-4").run(max_iterations=1)
        self.assertTrue(any("Corax status" in s and "gemma-4" in s for s in backend.sends))

    async def test_security_command_uses_chat_as_actor(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_cmd_update(5, "security", args="mode auto")]]
        )
        calls: list[tuple[str, str, str]] = []

        async def security(command, *, actor, transport):
            calls.append((command, actor, transport))
            return {"ok": True, "mode": "auto", "message": "security mode: auto"}

        await _gateway(backend, security_command=security).run(max_iterations=1)
        self.assertEqual(calls, [("mode auto", "5", "telegram")])
        self.assertIn("security mode: auto", backend.sends)

    async def test_approve_alias_resolves_single_pending_tool_without_exposing_result(self) -> None:
        backend = FakeBackend(
            poll_batches=[
                [_text_update(5, "delete file")],
                [{
                    "chat_id": 5,
                    "text": "/approve",
                    "command": {"is_command": True, "command": "unknown", "args": ""},
                }],
            ],
            llm_responses=[
                {"tool_calls": [_tool_call("filesystem", '{"operation": "delete"}')]},
                {"text": "Waiting for approval."},
            ],
        )
        backend.confirm_capability = "filesystem"
        calls: list[tuple[str, str, str]] = []

        async def security(command, *, actor, transport):
            calls.append((command, actor, transport))
            return {
                "ok": True,
                "status": "completed",
                "message": "task completed",
                "result": {"secret": "result-secret"},
            }

        await _gateway(backend, security_command=security).run(max_iterations=2)
        self.assertEqual(calls, [("approve task-confirm-1 once", "5", "telegram")])
        self.assertIn("✅ TOOL · filesystem · operation=delete · completed", backend.sends)
        self.assertNotIn("result-secret", "\n".join(backend.sends))

    async def test_deny_alias_resolves_single_pending_tool(self) -> None:
        backend = FakeBackend(
            poll_batches=[
                [_text_update(5, "delete file")],
                [{
                    "chat_id": 5,
                    "text": "/deny",
                    "command": {"is_command": True, "command": "unknown", "args": ""},
                }],
            ],
            llm_responses=[
                {"tool_calls": [_tool_call("filesystem", '{"operation": "delete"}')]},
                {"text": "Waiting for approval."},
            ],
        )
        backend.confirm_capability = "filesystem"
        calls: list[str] = []

        async def security(command, *, actor, transport):
            calls.append(command)
            return {"ok": True, "status": "cancelled", "message": "task denied"}

        await _gateway(backend, security_command=security).run(max_iterations=2)
        self.assertEqual(calls, ["deny task-confirm-1 once"])
        self.assertIn("❌ TOOL · filesystem · operation=delete · denied", backend.sends)

    async def test_approval_alias_forwards_explicit_scope(self) -> None:
        backend = FakeBackend()
        calls: list[str] = []

        async def security(command, *, actor, transport):
            calls.append(command)
            return {"ok": True, "message": "allowed", "resolution": "allow_session"}

        gw = _gateway(backend, security_command=security)
        gw._pending_approvals["5"] = {"task-1": "filesystem"}
        await gw.handle_update(_cmd_update(5, "approve", args="task-1 session"))
        self.assertEqual(calls, ["approve task-1 session"])

    async def test_full_security_approval_command_remains_compatible(self) -> None:
        backend = FakeBackend()
        calls: list[str] = []

        async def security(command, *, actor, transport):
            calls.append(command)
            return {"ok": True, "message": "task completed", "result": {"private": True}}

        gw = _gateway(backend, security_command=security)
        gw._pending_approvals["5"] = {"task-1": "filesystem"}
        await gw.handle_update(_cmd_update(5, "security", args="approve task-1"))
        self.assertEqual(calls, ["approve task-1"])
        self.assertIn("✅ TOOL · filesystem · completed", backend.sends)
        self.assertNotIn("private", "\n".join(backend.sends))

    async def test_approval_alias_requires_one_unambiguous_pending_task(self) -> None:
        backend = FakeBackend()
        calls: list[str] = []

        async def security(command, *, actor, transport):
            calls.append(command)
            return {"ok": True, "message": "unexpected"}

        gw = _gateway(backend, security_command=security)
        update = {
            "chat_id": 5,
            "text": "/approve",
            "command": {"is_command": True, "command": "unknown", "args": ""},
        }
        await gw.handle_update(update)
        self.assertIn("No pending approval", backend.sends[-1])
        gw._pending_approvals["5"] = {"task-1": "filesystem", "task-2": "shell"}
        await gw.handle_update(update)
        self.assertIn("More than one approval", backend.sends[-1])
        self.assertEqual(calls, [])

    async def test_failed_approval_keeps_pending_tool_and_shows_failure(self) -> None:
        backend = FakeBackend()

        async def security(command, *, actor, transport):
            raise GatewayError("private backend detail")

        gw = _gateway(backend, security_command=security)
        gw._pending_approvals["5"] = {"task-1": "filesystem"}
        await gw.handle_update({
            "chat_id": 5,
            "text": "/approve",
            "command": {"is_command": True, "command": "unknown", "args": ""},
        })
        visible = "\n".join(backend.sends)
        self.assertIn("❌ TOOL · filesystem · approval failed", visible)
        self.assertNotIn("private backend detail", visible)
        self.assertIn("task-1", gw._pending_approvals["5"])

    async def test_security_command_is_explicitly_unavailable_without_provider(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_cmd_update(5, "security", args="status")]]
        )
        await _gateway(backend).run(max_iterations=1)
        self.assertTrue(
            any("unavailable" in message.lower() for message in backend.sends)
        )

    async def test_bot_menu_is_synced_on_start(self) -> None:
        backend = FakeBackend(poll_batches=[[]])
        await _gateway(backend).run(max_iterations=1)
        self.assertTrue(
            any(cap_id == "telegram.connector" and op == "set_bot_commands" for cap_id, op, _ in backend.calls)
        )

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

    async def test_poll_offset_survives_gateway_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "telegram-gateway-state.json"
            first_backend = FakeBackend(poll_batches=[[]])
            await _gateway(
                first_backend,
                capabilities=CAPS_WITH_GATEWAY,
                state_path=state_path,
            ).run(max_iterations=1)

            second_backend = FakeBackend(poll_batches=[[]])
            await _gateway(
                second_backend,
                capabilities=CAPS_WITH_GATEWAY,
                state_path=state_path,
            ).run(max_iterations=1)

        poll_calls = [p for c, op, p in second_backend.calls if op == "poll"]
        self.assertEqual(poll_calls[0]["offset"], 999)

    async def test_poll_failure_is_logged_not_fatal(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]])
        backend.fail_capability = "telegram.connector"
        outcome = await _gateway(backend).run(max_iterations=1)
        self.assertEqual(outcome, "stopped")

    async def test_stopped_poll_failure_exits_quietly(self) -> None:
        backend = FakeBackend()
        gw = _gateway(backend)

        async def interrupted_poll():
            gw.stop()
            raise GatewayError("telegram API request interrupted")

        gw.poll_once = interrupted_poll
        outcome = await gw.run(max_iterations=1)
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


class HostRoleRoundTripTests(unittest.IsolatedAsyncioTestCase):
    """Infrastructure crosses host role ports and never enters the tool kernel."""

    async def asyncSetUp(self) -> None:
        if not HAS_CORE:
            self.skipTest("agent-core / agent-sdk not installed")
        if not TELEGRAM_REPO.is_dir():
            self.skipTest("corax-telegram-connector repo not present")
        if not LLM_REPO.is_dir():
            self.skipTest("corax-llm-local-connector repo not present")
        from corax import config as cfg
        from corax.runtime import CoraxRuntime

        config = cfg.default_config()
        config.extensions.available["telegram.connector"].path = str(TELEGRAM_REPO)
        config.extensions.available["llm.local"].path = str(LLM_REPO)
        self.runtime = CoraxRuntime(config, root_path=REPO_ROOT)
        await self.runtime.start()

    async def asyncTearDown(self) -> None:
        await self.runtime.stop()

    async def test_channel_invokes_through_host_port(self) -> None:
        output = await self.runtime.invoke_extension(
            "telegram.connector",
            {
                "operation": "poll",
                "mock": True,
                "mock_updates": [
                    {
                        "update_id": 1,
                        "message": {"text": "/new", "chat": {"id": 7}},
                    }
                ],
            },
        )
        self.assertEqual(output["count"], 1)
        self.assertEqual(output["updates"][0]["command"]["command"], "new_session")
        self.assertFalse(self.runtime.tools.has("telegram.connector"))

    async def test_host_port_raises_with_reason_on_failure(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            await self.runtime.invoke_extension(
                "telegram.connector",
                {"operation": "nope"},
            )
        self.assertIn("unsupported operation", str(ctx.exception))

    async def test_stream_generate_events_routes_from_model_provider(self) -> None:
        events = [
            event
            async for event in self.runtime.stream_extension(
                "llm.local",
                {"prompt": "hi", "mock_response": "hello"},
                session_id="stream-test",
            )
        ]
        self.assertIn({"type": "delta", "content": "hello"}, events)
        self.assertEqual(events[-1]["type"], "done")
        self.assertFalse(self.runtime.tools.has("llm.local"))


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
        async with engine.session([("fake.tool", FakeCap())], policy=ac.DefaultPolicyEngine()) as kernel:
            out = await kernel.invoke("fake.tool", {"x": 21}, wait_timeout=10)
            self.assertEqual(out, {"doubled": 42})  # payload echoed by the wrapper
            with self.assertRaises(KernelInvocationError) as ctx:
                await kernel.invoke("fake.tool", {"x": -1}, wait_timeout=10)
            self.assertIn("x must be non-negative", str(ctx.exception))  # error echoed too


if __name__ == "__main__":
    unittest.main()
