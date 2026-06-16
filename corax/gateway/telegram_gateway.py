"""Telegram chat gateway.

A platform-agnostic-ish loop that turns a Telegram chat into a conversation with
the agent: it polls for updates, dispatches slash commands, and streams the
model's reply back token-by-token by editing the message in place.

The loop logic here is **pure** — every side effect goes through two injected
async callables, so it unit-tests without a kernel, a network, or a real clock:

* ``run_capability(cap_id, payload, *, session_id=None) -> dict`` — execute a
  connector capability through the agent-core kernel and return its payload
  (read back from the core session state). Raises :class:`GatewayError` on a
  non-success task.
* ``stream_llm(payload, *, session_id) -> AsyncIterator[str]`` — stream the
  model's reply as text deltas (the one streaming path that goes straight to the
  ``llm.local`` instance, since the one-shot kernel cannot stream tokens).

Everything else — poll, send, the live edits — is routed through
``run_capability`` (i.e. through the core).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

RunCapability = Callable[..., Awaitable[dict]]
StreamLLM = Callable[..., AsyncIterator[str]]


class GatewayError(RuntimeError):
    """A capability task did not complete successfully through the kernel."""


class CoraxTelegramGateway:
    """Poll Telegram, dispatch commands, and stream model replies — via the core."""

    def __init__(
        self,
        *,
        run_capability: RunCapability,
        stream_llm: StreamLLM,
        llm_id: str = "llm.local",
        telegram_id: str = "telegram.connector",
        model: str | None = None,
        edit_interval_ms: int = 700,
        poll_timeout: int = 30,
        idle_sleep: float = 1.0,
        log: logging.Logger | None = None,
        new_session: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._run = run_capability
        self._stream_llm = stream_llm
        self.llm_id = llm_id
        self.telegram_id = telegram_id
        self.model = model
        self.edit_interval_ms = edit_interval_ms
        self.poll_timeout = poll_timeout
        self.idle_sleep = idle_sleep
        self.log = log or logging.getLogger("corax.gateway")
        self._new_session = new_session or (lambda: f"chat-{uuid.uuid4().hex[:8]}")
        self._now = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep

        self._sessions: dict[Any, str] = {}
        self._offset: int | None = None
        self._reload = False
        self._stop = False

    # -- public loop ----------------------------------------------------- #
    async def run(self, *, max_iterations: int | None = None) -> str:
        """Run the poll/dispatch loop. Returns ``"reload"`` or ``"stopped"``."""
        self._reload = False
        self._stop = False
        iterations = 0
        while not self._stop and not self._reload:
            try:
                updates = await self.poll_once()
            except Exception as exc:  # noqa: BLE001 - one bad poll must not kill the loop
                self.log.warning("poll failed: %s", exc)
                updates = []
            for update in updates:
                try:
                    await self.handle_update(update)
                except Exception as exc:  # noqa: BLE001 - one bad turn must not kill the loop
                    self.log.warning("failed handling update: %s", exc)
                if self._stop or self._reload:
                    break
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            if not updates and not self._stop and not self._reload:
                await self._sleep(self.idle_sleep)
        return "reload" if self._reload else "stopped"

    def stop(self) -> None:
        self._stop = True

    async def poll_once(self) -> list[dict]:
        payload: dict[str, Any] = {"operation": "poll", "timeout": self.poll_timeout}
        if self._offset is not None:
            payload["offset"] = self._offset
        result = await self._run(self.telegram_id, payload)
        next_offset = result.get("next_offset")
        if isinstance(next_offset, int):
            self._offset = next_offset
        updates = result.get("updates")
        return updates if isinstance(updates, list) else []

    async def handle_update(self, update: dict) -> None:
        chat_id = update.get("chat_id")
        if chat_id is None:
            return
        command = update.get("command") or {}
        if command.get("is_command"):
            await self._handle_command(chat_id, command)
            return
        text = update.get("text")
        if isinstance(text, str) and text.strip():
            await self._handle_chat(chat_id, text)

    # -- dispatch -------------------------------------------------------- #
    async def _handle_command(self, chat_id: Any, command: dict) -> None:
        name = command.get("command")
        if name == "new_session":
            self._sessions[chat_id] = self._new_session()
            await self._send(chat_id, command.get("reply") or "🆕 New session started.")
        elif name == "reload_agent":
            await self._send(chat_id, command.get("reply") or "♻️ Reloading the agent…")
            self._reload = True
        elif name == "set_model":
            args = command.get("args")
            if args:
                self.model = args
                await self._send(chat_id, f"✅ Model set to {args}")
            else:
                await self._send(chat_id, f"Current model: {self.model or 'default'}")
        elif name == "help":
            await self._send(chat_id, command.get("reply") or "Send a message to chat.")
        elif name == "cancel":
            await self._send(chat_id, command.get("reply") or "🛑 Cancelled.")
        else:  # unknown
            await self._send(chat_id, "Unknown command. Send /help.")

    async def _handle_chat(self, chat_id: Any, text: str) -> None:
        session_id = self._session_for(chat_id)
        payload: dict[str, Any] = {"prompt": text}
        if self.model:
            payload["model"] = self.model

        message_id: Any = None
        accumulated = ""
        last_sent = ""
        last_edit = self._now()
        try:
            async for chunk in self._stream_llm(payload, session_id=session_id):
                accumulated += chunk
                now = self._now()
                due = (now - last_edit) * 1000.0 >= self.edit_interval_ms
                if due and accumulated != last_sent:
                    message_id = await self._flush(
                        chat_id, message_id, accumulated, last_sent, done=False
                    )
                    last_sent = accumulated
                    last_edit = now
        except Exception as exc:  # noqa: BLE001 - never let one bad turn kill the loop
            self.log.warning("generation failed for chat %s: %s", chat_id, exc)
            accumulated = accumulated or "⚠️ generation failed"

        await self._flush(
            chat_id, message_id, accumulated or "(no response)", last_sent, done=True
        )

    # -- side effects (all through the kernel) --------------------------- #
    async def _flush(
        self, chat_id: Any, message_id: Any, text: str, last_sent: str, *, done: bool
    ) -> Any:
        payload = await self._run(
            self.telegram_id,
            {
                "operation": "stream",
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "last_sent_text": last_sent,
                "elapsed_ms": self.edit_interval_ms,
                "done": done,
            },
        )
        return payload.get("message_id", message_id)

    async def _send(self, chat_id: Any, text: str) -> None:
        await self._run(
            self.telegram_id, {"operation": "send", "chat_id": chat_id, "text": text}
        )

    def _session_for(self, chat_id: Any) -> str:
        session = self._sessions.get(chat_id)
        if session is None:
            session = self._new_session()
            self._sessions[chat_id] = session
        return session
