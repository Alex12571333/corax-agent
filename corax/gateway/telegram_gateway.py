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

import datetime
import json
import logging
from pathlib import Path
import re
import uuid
from typing import Any, Awaitable, Callable, Iterable

_DEFAULT_SYSTEM_PROMPT = (
    "You are Corax, a helpful assistant running locally on the user's machine. "
    "When the user asks you to act, use the available tools rather than guessing. "
    "For file creation, reading, editing, and deletion, prefer the filesystem or "
    "editor tools over shell commands. Reply in the user's language. "
    "If a tool fails, do not stop after the first error. Read the error, adjust "
    "the arguments, inspect the environment, or try a different available tool. "
    "Ask the user only when the next step requires information or permission "
    "that is not available from the current context. "
    "Only when the user explicitly asks you to send, attach, share, or upload a "
    "local file, use the telegram_send_document tool. Do not send files just "
    "because you created them. If the user asks for the file you just created "
    "or discussed, use the recent local files context to choose the matching path."
)

RunCapability = Callable[..., Awaitable[dict]]
ToolSelector = Callable[[str, list[dict]], Iterable[str]]

_SAFE_TOOL_NAME = re.compile(r"[^a-zA-Z0-9_-]")
_MEDIA_LINE = re.compile(r"^\s*MEDIA:\s*(?P<path>\S.*?)\s*$")
_MEDIA_REQUEST = re.compile(
    r"\b(send|attach|share|upload)\b|"
    r"(пришл|отправ|скинь|скинут|перешл|прикреп|загруз)",
    re.IGNORECASE,
)
_SEND_DOCUMENT_TOOL = "telegram_send_document"
_LOG_VALUE_LIMIT = 140


class GatewayError(RuntimeError):
    """A capability task did not complete successfully through the kernel."""


class CoraxTelegramGateway:
    """Poll Telegram, dispatch commands, and answer with a tool-calling loop."""

    def __init__(
        self,
        *,
        run_capability: RunCapability,
        capabilities: Iterable[dict] = (),
        gateway_id: str = "gateway",
        llm_id: str = "llm.local",
        telegram_id: str = "telegram.connector",
        model: str | None = None,
        poll_timeout: int = 30,
        idle_sleep: float = 1.0,
        max_tool_iterations: int = 6,
        max_tool_recovery_prompts: int = 1,
        max_tool_result_chars: int = 4000,
        reveal_chunk: int = 64,
        reveal_delay: float = 0.2,
        system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
        workspace_path: str | Path = "workspace",
        max_history_messages: int = 20,
        max_recent_files: int = 10,
        tool_selector: ToolSelector | None = None,
        max_active_tools: int = 8,
        log: logging.Logger | None = None,
        new_session: Callable[[], str] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._run = run_capability
        self.gateway_id = gateway_id
        self.llm_id = llm_id
        self.telegram_id = telegram_id
        self.model = model
        self.poll_timeout = poll_timeout
        self.idle_sleep = idle_sleep
        self.max_tool_iterations = max_tool_iterations
        self.max_tool_recovery_prompts = max(0, max_tool_recovery_prompts)
        self.max_tool_result_chars = max_tool_result_chars
        self.reveal_chunk = reveal_chunk
        self.reveal_delay = reveal_delay
        self.system_prompt = system_prompt
        self.workspace_path = Path(workspace_path).expanduser()
        self.max_history_messages = max(0, max_history_messages)
        self.max_recent_files = max(0, max_recent_files)
        self.tool_selector = tool_selector
        self.max_active_tools = max(1, max_active_tools)
        self.log = log or logging.getLogger("corax.gateway")
        self._new_session = new_session or (lambda: f"chat-{uuid.uuid4().hex[:8]}")
        self._sleep = sleep or _async_sleep

        # Build the tool catalogue from the registered capabilities, excluding
        # the chat infrastructure itself (the LLM and the Telegram connector).
        self._tool_specs: list[dict] = []
        self._tool_to_cap: dict[str, str] = {}
        self._cap_to_tool: dict[str, str] = {}
        self._send_document_spec: dict[str, Any] | None = None
        capability_list = list(capabilities)
        self._has_gateway_capability = any(cap.get("id") == gateway_id for cap in capability_list)
        has_telegram_connector = any(cap.get("id") == telegram_id for cap in capability_list)
        for cap in capability_list:
            cap_id = cap.get("id")
            if not cap_id or cap_id in (gateway_id, llm_id, telegram_id):
                continue
            name = _SAFE_TOOL_NAME.sub("_", cap_id)
            self._tool_to_cap[name] = cap_id
            self._cap_to_tool[cap_id] = name
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
        if has_telegram_connector:
            self._send_document_spec = {
                "type": "function",
                "function": {
                    "name": _SEND_DOCUMENT_TOOL,
                    "description": (
                        "Send a local file to the current Telegram chat. Use only after "
                        "the user explicitly asks to send, attach, share, or upload a file."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "caption": {"type": "string"},
                        },
                        "required": ["path"],
                    },
                },
            }
            self._tool_specs.append(self._send_document_spec)

        self._sessions: dict[Any, str] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._recent_files: dict[str, list[str]] = {}
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
            session_id = self._new_session()
            self._sessions[chat_id] = session_id
            if self._has_gateway_capability:
                try:
                    await self._run(
                        self.gateway_id,
                        {
                            "operation": "new_session",
                            "channel": "telegram",
                            "conversation_id": chat_id,
                            "session_id": session_id,
                        },
                    )
                except Exception as exc:  # noqa: BLE001 - command reply should still work
                    self.log.debug("gateway new_session failed: %s", exc)
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
        prepared = await self._prepare_turn(chat_id, text)
        session_id = prepared["session_id"]
        allow_media = bool(prepared["allow_outbound_file"])
        user_message = prepared["user_message"]
        messages = prepared["messages"]
        active_tools = self._active_tool_specs(text, allow_media=allow_media)
        final_text = ""
        recovery_needed = False
        recovery_tool_attempted = False
        recovery_prompts = 0
        recovery_prompt = ""
        for _ in range(self.max_tool_iterations):
            await self._typing(chat_id)  # show "typing…" while the agent works
            generate: dict[str, Any] = {
                "operation": "generate",
                "messages": messages,
                "tool_choice": "auto",
            }
            if active_tools:
                generate["tools"] = active_tools
            if self.model:
                generate["model"] = self.model
            response = await self._run(self.llm_id, generate, session_id=session_id)
            tool_calls = response.get("tool_calls") or []
            if not tool_calls:
                if (
                    recovery_needed
                    and not recovery_tool_attempted
                    and recovery_prompts < self.max_tool_recovery_prompts
                    and active_tools
                ):
                    recovery_prompts += 1
                    self.log.info("tool recovery prompt inserted after failed step")
                    messages.append({"role": "system", "content": recovery_prompt or self._tool_recovery_prompt()})
                    continue
                final_text = response.get("text") or ""
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": response.get("text") or None,
                    "tool_calls": tool_calls,
                }
            )
            failed_tools = 0
            for tool_call in tool_calls:
                tool_outcome = await self._run_tool(
                    messages,
                    tool_call,
                    chat_id=chat_id,
                    session_id=session_id,
                    allow_media=allow_media,
                    user_text=text,
                )
                if tool_outcome["failed"]:
                    failed_tools += 1
                    recovery_prompt = tool_outcome.get("recovery_prompt") or recovery_prompt
            if failed_tools:
                recovery_needed = True
                recovery_tool_attempted = False
            elif recovery_needed:
                recovery_needed = False
                recovery_tool_attempted = True
        else:
            final_text = "⚠️ Stopped: too many tool steps."
        delivered_text = await self._deliver_final(
            chat_id,
            final_text or "(no response)",
            allow_media=allow_media,
        )
        await self._record_turn(session_id, text, user_message, delivered_text)

    async def _typing(self, chat_id: Any) -> None:
        """Best-effort 'typing…' chat action; never let it break a turn."""
        try:
            await self._run(self.telegram_id, {"operation": "chat_action", "chat_id": chat_id})
        except Exception as exc:  # noqa: BLE001
            self.log.debug("chat action failed: %s", exc)

    async def _reveal(self, chat_id: Any, text: str) -> None:
        """Stream the final answer in by progressively editing one message."""
        if len(text) <= self.reveal_chunk:
            await self._send(chat_id, text)
            return
        message_id: Any = None
        last_sent = ""
        cut = self.reveal_chunk
        while cut < len(text):
            message_id = await self._stream_edit(chat_id, message_id, text[:cut], last_sent, done=False)
            last_sent = text[:cut]
            await self._sleep(self.reveal_delay)
            cut += self.reveal_chunk
        await self._stream_edit(chat_id, message_id, text, last_sent, done=True)

    async def _stream_edit(self, chat_id: Any, message_id: Any, text: str, last_sent: str, *, done: bool) -> Any:
        payload = await self._run(
            self.telegram_id,
            {
                "operation": "stream",
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "last_sent_text": last_sent,
                "elapsed_ms": 10 ** 9,  # force the connector to flush this edit
                "done": done,
            },
        )
        return payload.get("message_id", message_id)

    async def _run_tool(
        self,
        messages: list[dict],
        tool_call: dict,
        *,
        chat_id: Any,
        session_id: str,
        allow_media: bool,
        user_text: str,
    ) -> dict[str, Any]:
        function = tool_call.get("function") or {}
        name = function.get("name")
        cap_id = self._tool_to_cap.get(name)
        try:
            args = json.loads(function.get("arguments") or "{}")
            if not isinstance(args, dict):
                args = {}
        except (ValueError, TypeError):
            args = {}

        if name == _SEND_DOCUMENT_TOOL:
            result = await self._run_send_document_tool(
                chat_id,
                args,
                session_id=session_id,
                user_text=user_text,
                allow_media=allow_media,
            )
        elif cap_id is None:
            result: dict[str, Any] = {"error": f"unknown tool {name!r}"}
        else:
            self.log.info("tool %-20s %s", cap_id, self._format_tool_args_for_log(cap_id, args))
            self.log.debug("tool payload: %s(%s)", cap_id, args)
            try:
                result = await self._run(cap_id, args)
            except Exception as exc:  # noqa: BLE001 - a failed tool feeds the error back
                result = {"error": str(exc)}
            else:
                await self._record_artifact(session_id, cap_id, args, result)
            recovery_plan = await self._plan_tool_recovery(session_id, cap_id, args, result)
            tool_failed = bool(recovery_plan.get("tool_failed"))
            if tool_failed:
                self.log.warning(
                    "tool %-20s failed: %s",
                    cap_id,
                    self._compact_log_value(self._tool_error_text(result), limit=100),
                )
            model_result = recovery_plan.get("tool_result")
            if not isinstance(model_result, dict):
                model_result = result
        if name == _SEND_DOCUMENT_TOOL or cap_id is None:
            recovery_plan = await self._plan_tool_recovery(session_id, cap_id or name or "unknown", args, result)
            tool_failed = bool(recovery_plan.get("tool_failed"))
            model_result = recovery_plan.get("tool_result")
            if not isinstance(model_result, dict):
                model_result = result

        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.get("id"),
                "content": json.dumps(model_result)[: self.max_tool_result_chars],
            }
        )
        return {
            "failed": tool_failed,
            "recovery_prompt": recovery_plan.get("recovery_prompt") or "",
        }

    def _active_tool_specs(self, user_text: str, *, allow_media: bool) -> list[dict]:
        base_specs = [
            spec
            for spec in self._tool_specs
            if spec["function"]["name"] != _SEND_DOCUMENT_TOOL
        ]
        selected_specs = base_specs
        selected_ids: list[str] = []
        if self.tool_selector is not None:
            try:
                selected_ids = [
                    str(cap_id)
                    for cap_id in self.tool_selector(user_text, base_specs)
                    if str(cap_id) in self._cap_to_tool
                ]
            except Exception as exc:  # noqa: BLE001 - selector must not break chat
                self.log.debug("tool selector failed: %s", exc)
                selected_ids = []
            if selected_ids:
                selected_names = {self._cap_to_tool[cap_id] for cap_id in selected_ids}
                selected_specs = [
                    spec
                    for spec in base_specs
                    if spec["function"]["name"] in selected_names
                ]
            else:
                selected_specs = base_specs[: self.max_active_tools]
        else:
            selected_specs = base_specs

        if self.tool_selector is not None:
            selected_specs = selected_specs[: self.max_active_tools]

        if allow_media and self._send_document_spec is not None:
            selected_specs = [*selected_specs, self._send_document_spec]

        if selected_specs:
            self.log.debug(
                "active tools: %s",
                ", ".join(spec["function"]["name"] for spec in selected_specs),
            )
        return selected_specs

    async def _send(self, chat_id: Any, text: str) -> None:
        await self._run(
            self.telegram_id, {"operation": "send", "chat_id": chat_id, "text": text}
        )

    async def _deliver_final(self, chat_id: Any, text: str, *, allow_media: bool = True) -> str:
        clean_text, media_paths = self._extract_media_paths(text)
        await self._reveal(chat_id, clean_text or "(no response)")
        if allow_media:
            for media_path in media_paths:
                await self._send_document(chat_id, media_path)
        return clean_text or "(no response)"

    def _extract_media_paths(self, text: str) -> tuple[str, list[Path]]:
        lines: list[str] = []
        media_paths: list[Path] = []
        for line in text.splitlines():
            match = _MEDIA_LINE.match(line)
            if match is None:
                lines.append(line)
                continue
            media_paths.append(self._resolve_media_path(match.group("path")))
        if not media_paths:
            return text, media_paths
        return "\n".join(lines).strip(), media_paths

    async def _send_document(self, chat_id: Any, path: Path) -> None:
        try:
            await self._run(
                self.telegram_id,
                {
                    "operation": "send_document",
                    "chat_id": chat_id,
                    "path": str(path),
                    "caption": f"Файл: {path.name}",
                },
            )
        except Exception as exc:  # noqa: BLE001 - text delivery should still succeed
            self.log.debug("document send failed: %s", exc)

    async def _prepare_turn(self, chat_id: Any, text: str) -> dict[str, Any]:
        if self._has_gateway_capability:
            try:
                prepared = await self._run(
                    self.gateway_id,
                    {
                        "operation": "prepare_turn",
                        "channel": "telegram",
                        "conversation_id": chat_id,
                        "text": text,
                        "system_prompt": self.system_prompt,
                    },
                )
                if isinstance(prepared, dict) and prepared.get("messages"):
                    return prepared
            except Exception as exc:  # noqa: BLE001 - fallback keeps chat alive
                self.log.debug("gateway prepare_turn failed: %s", exc)

        session_id = self._session_for(chat_id)
        user_message = {"role": "user", "content": self._timestamped_user_message(text)}
        return {
            "session_id": session_id,
            "allow_outbound_file": self._user_requested_media(text),
            "user_message": user_message,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                *self._recent_files_context(session_id),
                *self._history_for(session_id),
                user_message,
            ],
        }

    async def _record_turn(
        self,
        session_id: str,
        text: str,
        user_message: dict[str, Any],
        assistant_text: str,
    ) -> None:
        if self._has_gateway_capability:
            try:
                await self._run(
                    self.gateway_id,
                    {
                        "operation": "record_turn",
                        "session_id": session_id,
                        "text": text,
                        "assistant_text": assistant_text,
                        "max_history_messages": self.max_history_messages,
                    },
                )
                return
            except Exception as exc:  # noqa: BLE001
                self.log.debug("gateway record_turn failed: %s", exc)
        self._remember_turn(session_id, user_message, assistant_text)

    async def _record_artifact(
        self, session_id: str, cap_id: str, args: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if self._has_gateway_capability:
            try:
                await self._run(
                    self.gateway_id,
                    {
                        "operation": "record_artifact",
                        "session_id": session_id,
                        "tool_capability_id": cap_id,
                        "tool_args": args,
                        "tool_result": result,
                        "max_recent_files": self.max_recent_files,
                    },
                )
                return
            except Exception as exc:  # noqa: BLE001
                self.log.debug("gateway record_artifact failed: %s", exc)
        self._remember_recent_file(session_id, cap_id, args, result)

    async def _plan_tool_recovery(
        self,
        session_id: str,
        cap_id: str,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if self._has_gateway_capability:
            try:
                planned = await self._run(
                    self.gateway_id,
                    {
                        "operation": "plan_tool_recovery",
                        "session_id": session_id,
                        "tool_capability_id": cap_id,
                        "tool_args": args,
                        "tool_result": result,
                        "max_tool_recovery_prompts": self.max_tool_recovery_prompts,
                    },
                )
                if (
                    isinstance(planned, dict)
                    and isinstance(planned.get("tool_failed"), bool)
                    and isinstance(planned.get("tool_result"), dict)
                ):
                    return planned
            except Exception as exc:  # noqa: BLE001
                self.log.debug("gateway plan_tool_recovery failed: %s", exc)

        return {
            "operation": "plan_tool_recovery",
            "ok": True,
            "tool_failed": self._tool_result_failed(result),
            "tool_result": json.loads(self._format_tool_result_for_model(result)),
            "recovery_prompt": self._tool_recovery_prompt() if self._tool_result_failed(result) else "",
        }

    async def _plan_file_delivery(
        self,
        session_id: str,
        user_text: str,
        args: dict[str, Any],
        *,
        allow_media: bool,
    ) -> dict[str, Any]:
        if self._has_gateway_capability:
            payload: dict[str, Any] = {
                "operation": "plan_file_delivery",
                "session_id": session_id,
                "text": user_text,
                "workspace_root": str(self.workspace_path),
                "connector_id": self.telegram_id,
            }
            raw_path = args.get("path")
            if isinstance(raw_path, str) and raw_path.strip():
                payload["path"] = raw_path
            caption = args.get("caption")
            if isinstance(caption, str) and caption.strip():
                payload["caption"] = caption
            try:
                planned = await self._run(self.gateway_id, payload)
                delivery = planned.get("delivery") if isinstance(planned, dict) else None
                if isinstance(delivery, dict):
                    return delivery
            except Exception as exc:  # noqa: BLE001
                self.log.debug("gateway plan_file_delivery failed: %s", exc)

        if not allow_media:
            return {"allowed": False, "reason": "file delivery was not requested by the user"}
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return {"allowed": False, "reason": "path must be a non-empty string"}
        return {"allowed": True, "path": str(self._resolve_media_path(raw_path))}

    async def _run_send_document_tool(
        self,
        chat_id: Any,
        args: dict[str, Any],
        *,
        session_id: str,
        user_text: str,
        allow_media: bool,
    ) -> dict[str, Any]:
        delivery_plan = await self._plan_file_delivery(session_id, user_text, args, allow_media=allow_media)
        if not delivery_plan.get("allowed"):
            return {
                "ok": False,
                "error": delivery_plan.get("reason") or "file delivery was not allowed",
            }
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raw_path = delivery_plan.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return {"ok": False, "error": "path must be a non-empty string"}
        path = Path(delivery_plan["path"]) if delivery_plan.get("path") else self._resolve_media_path(raw_path)
        payload: dict[str, Any] = {
            "operation": "send_document",
            "chat_id": chat_id,
            "path": str(path),
        }
        caption = args.get("caption")
        if isinstance(caption, str) and caption.strip():
            payload["caption"] = caption
        else:
            payload["caption"] = f"Файл: {path.name}"
        try:
            result = await self._run(self.telegram_id, payload)
        except Exception as exc:  # noqa: BLE001 - tool result feeds the error back
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(path), "result": result}

    def _timestamped_user_message(self, text: str) -> str:
        now = datetime.datetime.now().astimezone()
        timestamp = now.strftime("%a %Y-%m-%d %H:%M %Z")
        return f"[{timestamp}] {text}"

    def _format_tool_args_for_log(self, cap_id: str, args: dict[str, Any]) -> str:
        if not args:
            return ""
        if cap_id == "shell":
            command = args.get("command")
            if isinstance(command, str):
                return f'command="{self._compact_log_value(command)}"'
        if cap_id in {"filesystem", "editor"}:
            parts = []
            operation = args.get("operation")
            path = args.get("path")
            if isinstance(operation, str) and operation:
                parts.append(f"operation={operation}")
            if isinstance(path, str) and path:
                parts.append(f'path="{self._compact_log_value(path, limit=80)}"')
            if parts:
                return " ".join(parts)

        parts = []
        for key, value in args.items():
            if key in {"content", "text", "last_sent_text"}:
                continue
            if isinstance(value, str):
                parts.append(f'{key}="{self._compact_log_value(value, limit=80)}"')
            elif isinstance(value, (int, float, bool)) or value is None:
                parts.append(f"{key}={value!r}")
            else:
                parts.append(f"{key}=<{type(value).__name__}>")
            if len(parts) >= 4:
                break
        return " ".join(parts) if parts else f"{len(args)} arg(s)"

    def _format_tool_result_for_model(self, result: dict[str, Any]) -> str:
        if not self._tool_result_failed(result):
            return json.dumps(result)
        payload = dict(result)
        payload.setdefault("ok", False)
        error_text = self._tool_error_text(result)
        error_kind = self._classify_tool_error(error_text)
        payload["error_kind"] = error_kind
        payload["recovery_steps"] = self._recovery_steps_for_error(error_kind)
        payload["recovery_hint"] = (
            "The tool call failed. Diagnose the error and continue autonomously: "
            "fix the arguments, inspect the environment, or try another available "
            "tool before asking the user. Ask the user only if required information "
            "or permission is genuinely missing."
        )
        return json.dumps(payload)

    def _tool_recovery_prompt(self) -> str:
        return (
            "A tool failed on the previous step and no recovery attempt has been "
            "made yet. Continue the task autonomously: call an available tool to "
            "inspect the environment, verify assumptions, retry with corrected "
            "arguments, or choose a safer alternate route. Do not ask the user to "
            "choose an option unless required information or permission is missing."
        )

    def _tool_result_failed(self, result: dict[str, Any]) -> bool:
        if result.get("ok") is False:
            return True
        return any(key in result for key in ("error", "errors", "exception"))

    def _tool_error_text(self, result: dict[str, Any]) -> str:
        for key in ("error", "exception", "message"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
        errors = result.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(error) for error in errors[:3])
        return "unknown error"

    def _classify_tool_error(self, error_text: str) -> str:
        text = error_text.lower()
        if any(marker in text for marker in ("file not found", "no such file", "not found")):
            return "missing_file_or_resource"
        if any(marker in text for marker in ("permission denied", "not permitted", "forbidden", "unauthorized")):
            return "permission_denied"
        if any(
            marker in text
            for marker in ("module not found", "modulenotfounderror", "no module named", "importerror")
        ):
            return "missing_dependency"
        if any(marker in text for marker in ("command not found", "not recognized", "executable not found")):
            return "missing_command"
        if any(marker in text for marker in ("timed out", "timeout", "deadline")):
            return "timeout"
        if any(marker in text for marker in ("connection", "dns", "network", "ssl", "http")):
            return "network_or_remote"
        if any(marker in text for marker in ("invalid", "schema", "argument", "required")):
            return "bad_arguments"
        return "unknown"

    def _recovery_steps_for_error(self, error_kind: str) -> list[str]:
        recipes = {
            "missing_file_or_resource": [
                "list or search nearby paths",
                "check recent local files context",
                "retry with the corrected path or explain if absent",
            ],
            "permission_denied": [
                "try a read-only inspection command",
                "avoid destructive escalation",
                "ask the user only if permission is required",
            ],
            "missing_dependency": [
                "check whether an alternate installed tool or stdlib path exists",
                "inspect the environment",
                "install only if the available policy/tooling permits it",
            ],
            "missing_command": [
                "check command availability",
                "use an alternate command or capability",
                "retry with the available tool",
            ],
            "timeout": [
                "retry with a narrower query or smaller workload",
                "inspect partial state if available",
                "use an alternate route",
            ],
            "network_or_remote": [
                "retry with headers or a simpler request",
                "try an alternate endpoint/source if available",
                "summarize uncertainty if remote access is blocked",
            ],
            "bad_arguments": [
                "compare the payload with the tool schema",
                "remove unsupported fields or add required fields",
                "retry with corrected arguments",
            ],
        }
        return recipes.get(
            error_kind,
            [
                "inspect the error and current context",
                "try a smaller or safer tool call",
                "ask the user only if blocked by missing information",
            ],
        )

    def _compact_log_value(self, value: str, *, limit: int = _LOG_VALUE_LIMIT) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) <= limit:
            return cleaned
        return f"{cleaned[: limit - 1]}..."

    def _user_requested_media(self, text: str) -> bool:
        return bool(_MEDIA_REQUEST.search(text))

    def _resolve_media_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.workspace_path / path
        return path

    def _recent_files_context(self, session_id: str) -> list[dict[str, str]]:
        files = self._recent_files.get(session_id, [])
        if not files:
            return []
        file_list = "\n".join(f"- {path}" for path in files)
        return [
            {
                "role": "system",
                "content": (
                    "Recent local files created or modified in this session "
                    "(newest first):\n"
                    f"{file_list}\n"
                    "When the user asks for the file just created, discussed, "
                    "or requested earlier, use telegram_send_document with the "
                    "matching path."
                ),
            }
        ]

    def _remember_recent_file(
        self, session_id: str, cap_id: str, args: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if self.max_recent_files <= 0:
            return
        path = self._artifact_path_from_tool(cap_id, args, result)
        if path is None:
            return
        files = self._recent_files.setdefault(session_id, [])
        if path in files:
            files.remove(path)
        files.insert(0, path)
        del files[self.max_recent_files :]

    def _artifact_path_from_tool(
        self, cap_id: str, args: dict[str, Any], result: dict[str, Any]
    ) -> str | None:
        raw_path = result.get("path") if isinstance(result.get("path"), str) else args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None

        operation = str(args.get("operation") or result.get("operation") or "").lower()
        if cap_id == "filesystem":
            if operation not in {"write", "append"} and not (
                result.get("written") or result.get("appended")
            ):
                return None
        elif cap_id == "editor":
            if result.get("success") is False or result.get("changed") is False:
                return None
        else:
            return None
        return raw_path.strip()

    def _history_for(self, session_id: str) -> list[dict[str, Any]]:
        return [dict(message) for message in self._history.get(session_id, [])]

    def _remember_turn(
        self, session_id: str, user_message: dict[str, Any], assistant_text: str
    ) -> None:
        if self.max_history_messages <= 0:
            return
        history = self._history.setdefault(session_id, [])
        history.extend(
            [
                dict(user_message),
                {"role": "assistant", "content": assistant_text},
            ]
        )
        overflow = len(history) - self.max_history_messages
        if overflow > 0:
            del history[:overflow]

    def _session_for(self, chat_id: Any) -> str:
        session = self._sessions.get(chat_id)
        if session is None:
            session = self._new_session()
            self._sessions[chat_id] = session
        return session


async def _async_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
