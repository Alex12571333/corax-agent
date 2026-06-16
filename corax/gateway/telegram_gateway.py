"""Telegram chat gateway with tool-calling.

Turns a Telegram chat into an agent: it polls for updates, dispatches slash
commands, and answers messages by running a tool-calling loop — the model may
call **any** of the agent's capabilities (filesystem, editor, shell, and any
future one), each executed **through the agent-core kernel**, with the result
fed back to the model until it produces a final answer.

The loop logic is pure: every side effect goes through one injected async
callable ``run_capability(cap_id, payload, *, session_id=None) -> dict`` (a
kernel ``invoke``), so it unit-tests without a kernel or network. Capabilities
exposed as tools are built from the injected ``capabilities`` list — so new
capabilities become available automatically, with no per-tool wiring.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Awaitable, Callable, Iterable

RunCapability = Callable[..., Awaitable[dict]]

_SAFE_TOOL_NAME = re.compile(r"[^a-zA-Z0-9_-]")


class GatewayError(RuntimeError):
    """A capability task did not complete successfully through the kernel."""


class CoraxTelegramGateway:
    """Poll Telegram, dispatch commands, and answer with a tool-calling loop."""

    def __init__(
        self,
        *,
        run_capability: RunCapability,
        capabilities: Iterable[dict] = (),
        llm_id: str = "llm.local",
        telegram_id: str = "telegram.connector",
        model: str | None = None,
        poll_timeout: int = 30,
        idle_sleep: float = 1.0,
        max_tool_iterations: int = 6,
        max_tool_result_chars: int = 4000,
        log: logging.Logger | None = None,
        new_session: Callable[[], str] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._run = run_capability
        self.llm_id = llm_id
        self.telegram_id = telegram_id
        self.model = model
        self.poll_timeout = poll_timeout
        self.idle_sleep = idle_sleep
        self.max_tool_iterations = max_tool_iterations
        self.max_tool_result_chars = max_tool_result_chars
        self.log = log or logging.getLogger("corax.gateway")
        self._new_session = new_session or (lambda: f"chat-{uuid.uuid4().hex[:8]}")
        self._sleep = sleep or _async_sleep

        # Build the tool catalogue from the registered capabilities, excluding
        # the chat infrastructure itself (the LLM and the Telegram connector).
        self._tool_specs: list[dict] = []
        self._tool_to_cap: dict[str, str] = {}
        for cap in capabilities:
            cap_id = cap.get("id")
            if not cap_id or cap_id in (llm_id, telegram_id):
                continue
            name = _SAFE_TOOL_NAME.sub("_", cap_id)
            self._tool_to_cap[name] = cap_id
            params = cap.get("input_schema") or {}
            if not params:
                params = {"type": "object", "properties": {}}
            self._tool_specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": cap.get("description") or cap_id,
                        "parameters": params,
                    },
                }
            )

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
        messages: list[dict[str, Any]] = [{"role": "user", "content": text}]
        final_text = ""
        for _ in range(self.max_tool_iterations):
            generate: dict[str, Any] = {
                "operation": "generate",
                "messages": messages,
                "tool_choice": "auto",
            }
            if self._tool_specs:
                generate["tools"] = self._tool_specs
            if self.model:
                generate["model"] = self.model
            response = await self._run(self.llm_id, generate, session_id=session_id)
            tool_calls = response.get("tool_calls") or []
            if not tool_calls:
                final_text = response.get("text") or ""
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": response.get("text") or None,
                    "tool_calls": tool_calls,
                }
            )
            for tool_call in tool_calls:
                await self._run_tool(messages, tool_call)
        else:
            final_text = "⚠️ Stopped: too many tool steps."
        await self._send(chat_id, final_text or "(no response)")

    async def _run_tool(self, messages: list[dict], tool_call: dict) -> None:
        function = tool_call.get("function") or {}
        name = function.get("name")
        cap_id = self._tool_to_cap.get(name)
        try:
            args = json.loads(function.get("arguments") or "{}")
            if not isinstance(args, dict):
                args = {}
        except (ValueError, TypeError):
            args = {}

        if cap_id is None:
            result: dict[str, Any] = {"error": f"unknown tool {name!r}"}
        else:
            self.log.info("tool call: %s(%s)", cap_id, args)
            try:
                result = await self._run(cap_id, args)
            except Exception as exc:  # noqa: BLE001 - a failed tool feeds the error back
                result = {"error": str(exc)}

        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.get("id"),
                "content": json.dumps(result)[: self.max_tool_result_chars],
            }
        )

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


async def _async_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
