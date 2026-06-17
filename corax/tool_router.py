"""LLM-based tool router.

A more capable replacement for the lexical top-K selector. Instead of matching
hand-maintained keyword lists, it asks the LLM itself which tools (if any) a
user message needs — so it handles paraphrase, any language, and composite
intents ("find the weather *and* save it to a file") without per-tool wiring.

It is deliberately cheap: one short, temperature-0 completion that must return
only a JSON array of tool ids. It never breaks a turn — on a model error,
timeout, or unparseable reply it falls back to an injected lexical selector (and
to "no opinion" if none is given, which the gateway reads as "offer all tools").

The LLM call is injected as an async ``run_capability(cap_id, payload) -> dict``
(the kernel ``invoke``), so the router unit-tests without a kernel or network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Awaitable, Callable, Iterable, Sequence

RunCapability = Callable[..., Awaitable[dict]]
LexicalSelector = Callable[[str, list[dict]], Iterable[str]]

# Matches a single (non-nested) JSON array, e.g. ["filesystem", "web.search"].
_JSON_ARRAY = re.compile(r"\[[^\[\]]*\]", re.DOTALL)

_ROUTER_SYSTEM = (
    "You are the tool router for a local AI assistant. Given the user message "
    "and the list of available tools, decide which tools are needed to fulfil "
    'the message. Reply with ONLY a JSON array of tool ids, e.g. ["filesystem"]. '
    "Rules:\n"
    "- Return [] when no tool is needed: greetings, small talk, opinions, or "
    "facts that are stable and that you already know (math, definitions, history).\n"
    "- Anything time-sensitive or that you cannot know precisely from memory "
    "REQUIRES a web search: today's or tomorrow's weather, current news, prices, "
    "exchange rates, schedules, scores, 'latest/current/now/today/tomorrow', or "
    "any fact about the real world right now. Do not answer these from memory.\n"
    "- Local files, listing/reading/writing/deleting files, and running commands "
    "use the file/shell tools, never web search.\n"
    "- Include every tool a multi-step task needs (for example searching the web "
    "AND writing a file).\n"
    "- Choose only ids from the provided list; never invent one. Output the JSON "
    "array and nothing else."
)


class LLMToolRouter:
    """Pick the active tool set for a turn by asking the LLM."""

    def __init__(
        self,
        run_capability: RunCapability,
        *,
        catalog: Sequence[dict],
        llm_id: str = "llm.local",
        model: str | None = None,
        fallback: LexicalSelector | None = None,
        top_k: int = 8,
        timeout: float = 12.0,
        max_tokens: int = 64,
        log: logging.Logger | None = None,
    ) -> None:
        self._run = run_capability
        self.llm_id = llm_id
        self.model = model
        self.fallback = fallback
        self.top_k = max(1, top_k)
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.log = log or logging.getLogger("corax.tool_router")

        self._ids = [str(cap["id"]) for cap in catalog if cap.get("id")]
        self._id_set = set(self._ids)
        # Accept both the dotted id and its safe-name form (web.search/web_search).
        self._alias: dict[str, str] = {}
        for cap_id in self._ids:
            self._alias[cap_id] = cap_id
            self._alias[cap_id.replace(".", "_")] = cap_id
        self._menu = _render_menu(catalog)

    async def route(self, user_text: str, specs: list[dict]) -> list[str]:
        """Return the selected capability ids ([] means 'no tool needed')."""
        try:
            ids = await asyncio.wait_for(self._route_via_llm(user_text), self.timeout)
        except Exception as exc:  # noqa: BLE001 - routing (incl. timeout) must never break a turn
            self.log.debug("llm tool router failed (%s); using lexical fallback", exc)
            return self._fallback(user_text, specs)
        if ids is None:
            self.log.debug("llm tool router gave no parseable ids; using lexical fallback")
            return self._fallback(user_text, specs)
        return ids[: self.top_k]

    async def _route_via_llm(self, user_text: str) -> list[str] | None:
        payload: dict = {
            "operation": "generate",
            "messages": [
                {"role": "system", "content": f"{_ROUTER_SYSTEM}\n\n{self._menu}"},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
        }
        if self.model:
            payload["model"] = self.model
        result = await self._run(self.llm_id, payload)
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str):
            return None
        return self._parse_ids(text)

    def _parse_ids(self, text: str) -> list[str] | None:
        match = _JSON_ARRAY.search(text)
        if match is None:
            return None
        try:
            raw = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, list):
            return None
        selected: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            cap_id = self._alias.get(item.strip())
            if cap_id and cap_id not in selected:
                selected.append(cap_id)
        return selected

    def _fallback(self, user_text: str, specs: list[dict]) -> list[str]:
        if self.fallback is None:
            return []
        try:
            return [
                str(cap_id)
                for cap_id in self.fallback(user_text, specs)
                if str(cap_id) in self._id_set
            ][: self.top_k]
        except Exception as exc:  # noqa: BLE001 - fallback must not break a turn
            self.log.debug("lexical fallback failed: %s", exc)
            return []


def _render_menu(catalog: Sequence[dict]) -> str:
    lines = ["Tools:"]
    for cap in catalog:
        cap_id = str(cap.get("id") or "")
        if not cap_id:
            continue
        description = " ".join(str(cap.get("description") or "").split())
        operations = _operations(cap)
        line = f"- {cap_id}: {description}" if description else f"- {cap_id}"
        if operations:
            line += f" (operations: {', '.join(operations)})"
        lines.append(line)
    return "\n".join(lines)


def _operations(cap: dict) -> list[str]:
    schema = cap.get("input_schema") or {}
    properties = schema.get("properties") or {}
    operation = properties.get("operation") or {}
    enum = operation.get("enum")
    return [str(value) for value in enum] if isinstance(enum, list) else []
