"""Gateway: command dispatch, streaming chat loop, and the core policy.

The loop logic is exercised with injected fakes — no kernel, no network, no real
clock — so the tests are fast and deterministic.
"""

from __future__ import annotations

import asyncio
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


class FakeBackend:
    """Records connector calls and serves scripted poll results / llm streams."""

    def __init__(self, poll_batches=None, llm_chunks=None):
        self.poll_batches = list(poll_batches or [])
        self.llm_chunks = list(llm_chunks if llm_chunks is not None else ["Hel", "lo"])
        self.calls: list[tuple[str, dict]] = []
        self.sends: list[str] = []
        self.fail_capability: str | None = None
        self._mid = 0

    async def run_capability(self, cap_id, payload, *, session_id=None):
        self.calls.append((payload.get("operation"), payload))
        if cap_id == self.fail_capability:
            raise GatewayError("boom")
        op = payload.get("operation")
        if op == "poll":
            batch = self.poll_batches.pop(0) if self.poll_batches else []
            return {"updates": batch, "next_offset": 999}
        if op == "send":
            self.sends.append(payload["text"])
            self._mid += 1
            return {"message_id": self._mid}
        if op == "stream":
            self._mid += 1 if payload.get("message_id") is None else 0
            return {"edited": True, "message_id": payload.get("message_id") or self._mid}
        return {}

    async def stream_llm(self, payload, *, session_id):
        for chunk in self.llm_chunks:
            yield chunk


def _clock():
    """A monotonic fake clock that jumps 10s per call (always 'due' to edit)."""
    t = {"v": 0.0}

    def now():
        t["v"] += 10.0
        return t["v"]

    return now


async def _nosleep(_seconds):
    return None


def _text_update(chat_id, text):
    return {"chat_id": chat_id, "text": text, "command": {"is_command": False}}


def _cmd_update(chat_id, command, args="", reply=None):
    return {
        "chat_id": chat_id,
        "text": f"/{command}",
        "command": {"is_command": True, "command": command, "args": args, "reply": reply},
    }


def _gateway(backend, **kwargs):
    return CoraxTelegramGateway(
        run_capability=backend.run_capability,
        stream_llm=backend.stream_llm,
        clock=_clock(),
        sleep=_nosleep,
        new_session=lambda: "sess-fixed",
        **kwargs,
    )


class GatewayPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_allows_confirm_denies_dangerous(self) -> None:
        from agent_core import PermissionLevel, RiskLevel, SideEffect
        from agent_core.policy.base import PolicyContext

        policy = GatewayPolicyEngine()

        def ctx(level):
            return PolicyContext(
                session_id="s", capability_id="c", permission_level=level,
                risk_level=RiskLevel.MEDIUM, side_effects={SideEffect.NETWORK_REQUEST},
                required_scopes=set(),
            )

        allow = await policy.evaluate(None, None, ctx(PermissionLevel.CONFIRM))
        deny = await policy.evaluate(None, None, ctx(PermissionLevel.DANGEROUS))
        self.assertTrue(allow.allowed)
        self.assertFalse(deny.allowed)


class GatewayChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_streams_reply_via_connector(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_text_update(5, "hi there")]],
            llm_chunks=["Hel", "lo", " world"],
        )
        gw = _gateway(backend, model="gemma-4")  # exercises the model-in-payload branch
        outcome = await gw.run(max_iterations=1)
        self.assertEqual(outcome, "stopped")
        stream_calls = [p for op, p in backend.calls if op == "stream"]
        self.assertTrue(stream_calls)
        final = stream_calls[-1]
        self.assertTrue(final["done"])
        self.assertEqual(final["text"], "Hello world")

    async def test_empty_llm_yields_placeholder(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]], llm_chunks=[])
        gw = _gateway(backend)
        await gw.run(max_iterations=1)
        final = [p for op, p in backend.calls if op == "stream"][-1]
        self.assertEqual(final["text"], "(no response)")

    async def test_generation_error_is_caught(self) -> None:
        async def boom_stream(payload, *, session_id):
            raise RuntimeError("model down")
            yield  # pragma: no cover

        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]])
        gw = CoraxTelegramGateway(
            run_capability=backend.run_capability, stream_llm=boom_stream,
            clock=_clock(), sleep=_nosleep,
        )
        await gw.run(max_iterations=1)
        final = [p for op, p in backend.calls if op == "stream"][-1]
        self.assertIn("failed", final["text"])

    async def test_throttle_skips_mid_edits(self) -> None:
        # A clock that never advances => never "due" => only the final flush.
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]], llm_chunks=["a", "b", "c"])
        gw = CoraxTelegramGateway(
            run_capability=backend.run_capability, stream_llm=backend.stream_llm,
            clock=lambda: 1.0, sleep=_nosleep, new_session=lambda: "s",
        )
        await gw.run(max_iterations=1)
        stream_calls = [p for op, p in backend.calls if op == "stream"]
        self.assertEqual(len(stream_calls), 1)  # final only
        self.assertEqual(stream_calls[0]["text"], "abc")


class GatewayCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_resets_and_replies(self) -> None:
        backend = FakeBackend(poll_batches=[[_cmd_update(5, "new_session", reply="🆕")]])
        gw = _gateway(backend)
        await gw.run(max_iterations=1)
        self.assertIn("🆕", backend.sends)

    async def test_reload_sets_outcome(self) -> None:
        backend = FakeBackend(poll_batches=[[_cmd_update(5, "reload_agent", reply="reloading")]])
        gw = _gateway(backend)
        outcome = await gw.run(max_iterations=5)
        self.assertEqual(outcome, "reload")
        self.assertIn("reloading", backend.sends)

    async def test_set_model_with_and_without_args(self) -> None:
        backend = FakeBackend(
            poll_batches=[[_cmd_update(5, "set_model", args="gemma-4"), _cmd_update(5, "set_model", args="")]]
        )
        gw = _gateway(backend)
        await gw.run(max_iterations=1)
        self.assertEqual(gw.model, "gemma-4")
        self.assertTrue(any("gemma-4" in s for s in backend.sends))
        self.assertTrue(any("Current model" in s for s in backend.sends))

    async def test_help_cancel_and_unknown(self) -> None:
        backend = FakeBackend(
            poll_batches=[[
                _cmd_update(5, "help", reply="HELP"),
                _cmd_update(5, "cancel", reply="🛑"),
                {"chat_id": 5, "text": "/wat", "command": {"is_command": True, "command": "unknown"}},
            ]]
        )
        gw = _gateway(backend)
        await gw.run(max_iterations=1)
        self.assertIn("HELP", backend.sends)
        self.assertIn("🛑", backend.sends)
        self.assertTrue(any("Unknown command" in s for s in backend.sends))


class GatewayLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_without_chat_id_skipped(self) -> None:
        backend = FakeBackend(poll_batches=[[{"chat_id": None, "text": "x", "command": {}}]])
        gw = _gateway(backend)
        await gw.run(max_iterations=1)
        self.assertEqual(backend.sends, [])

    async def test_blank_text_skipped(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "   ")]])
        gw = _gateway(backend)
        await gw.run(max_iterations=1)
        self.assertFalse([p for op, p in backend.calls if op == "stream"])

    async def test_idle_then_stop(self) -> None:
        backend = FakeBackend(poll_batches=[[]])  # one empty poll
        gw = _gateway(backend)
        # max_iterations stops the loop after the idle sleep path runs once.
        outcome = await gw.run(max_iterations=1)
        self.assertEqual(outcome, "stopped")

    async def test_stop_breaks_loop(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")], [_text_update(5, "again")]])

        async def stream_and_stop(payload, *, session_id):
            gw.stop()
            yield "x"

        gw = CoraxTelegramGateway(
            run_capability=backend.run_capability, stream_llm=stream_and_stop,
            clock=_clock(), sleep=_nosleep, new_session=lambda: "s",
        )
        outcome = await gw.run(max_iterations=10)
        self.assertEqual(outcome, "stopped")

    async def test_poll_offset_threaded(self) -> None:
        backend = FakeBackend(poll_batches=[[], []])
        gw = _gateway(backend)
        await gw.run(max_iterations=2)
        poll_calls = [p for op, p in backend.calls if op == "poll"]
        self.assertEqual(poll_calls[1]["offset"], 999)  # offset from first poll reused

    async def test_run_capability_failure_propagates(self) -> None:
        backend = FakeBackend(poll_batches=[[_text_update(5, "hi")]])
        backend.fail_capability = "telegram.connector"
        gw = _gateway(backend)
        with self.assertRaises(GatewayError):
            await gw.run(max_iterations=1)


class GatewayCoreRoundTripTests(unittest.IsolatedAsyncioTestCase):
    """Run a connector through the real kernel under the gateway policy and read
    its output back from core session state (the gateway's data path)."""

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

    async def test_poll_through_kernel_reads_state(self) -> None:
        from agent_core import TaskStatus

        async with self.runtime.core.session(
            self.runtime.capabilities, policy=GatewayPolicyEngine()
        ) as kernel:
            task = await kernel.run_task(
                required_capability="telegram.connector",
                input={
                    "operation": "poll",
                    "mock": True,
                    "mock_updates": [
                        {"update_id": 1, "message": {"text": "/new", "chat": {"id": 7}}}
                    ],
                    "state_key": "gw_output",
                },
                session_id="gw-test",
                wait_timeout=10,
            )
            self.assertIs(task.status, TaskStatus.COMPLETED)
            state = await kernel.get_state("gw-test")
            output = state.temporary_context["gw_output"]
            self.assertEqual(output["count"], 1)
            self.assertEqual(output["updates"][0]["command"]["command"], "new_session")


if __name__ == "__main__":
    unittest.main()
