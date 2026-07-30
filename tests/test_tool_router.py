"""Embedding-first tool catalog, selection and per-turn activation."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from corax.config import ToolRoutingConfig
from corax.tool_router import (
    TOOL_CALL_ID,
    TOOL_SEARCH_ID,
    ToolRoutingHost,
    is_trivial_chitchat,
)


def _tool(
    extension_id: str,
    *,
    summary: str,
    intents: list[str],
    examples: list[str],
    anti_examples: list[str] | None = None,
    channels: list[str] | None = None,
    permission_level: str = "safe",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=extension_id,
        name=extension_id.title(),
        description=summary,
        version="1.0.0",
        tags={"tool"},
        permission_level=permission_level,
        required_scopes=set(),
        risk_level="low",
        side_effects={"none"},
        input_schema={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["run"]},
                "value": {"type": "string"},
            },
        },
        routing={
            "summary": summary,
            "intents": intents,
            "examples": examples,
            "anti_examples": anti_examples or [],
            "channels": channels or ["console", "tui", "telegram"],
        },
    )


class FakeEmbeddings:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def embed(self, texts, *, input_type):
        self.calls.append((input_type, tuple(texts)))
        if self.fail:
            raise OSError("offline")
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> tuple[float, float, float]:
        lowered = text.lower()
        if any(word in lowered for word in ("file", "файл", "document")):
            return (1.0, 0.0, 0.0)
        if any(word in lowered for word in ("web", "news", "интернет", "новост")):
            return (0.0, 1.0, 0.0)
        if "cron" in lowered or "schedule" in lowered:
            return (0.0, 0.0, 1.0)
        return (0.1, 0.1, 0.1)


def _host(client: FakeEmbeddings | None = None) -> ToolRoutingHost:
    config = ToolRoutingConfig(
        dimension=3,
        top_k=1,
        max_active_tools=4,
        max_schema_bytes=20_000,
        min_similarity=0.2,
    )
    host = ToolRoutingHost(config, client=client or FakeEmbeddings())
    host.sync(
        [
            (
                "filesystem",
                _tool(
                    "filesystem",
                    summary="Read and write workspace files",
                    intents=["local file operations"],
                    examples=["прочитай файл config.yaml"],
                ),
            ),
            (
                "web.search",
                _tool(
                    "web.search",
                    summary="Search current information on the web",
                    intents=["internet research and current news"],
                    examples=["найди последние новости"],
                ),
            ),
        ]
    )
    return host


class ToolRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_selection_exposes_only_selected_schema_and_search(self):
        host = _host()
        turn = await host.begin_turn(
            "прочитай файл config.yaml",
            session_id="s1",
            turn_id="t1",
            channel="console",
        )

        self.assertEqual(turn.active_ids, [TOOL_SEARCH_ID, "filesystem"])
        names = {
            item["function"]["name"]
            for item in host.active_schemas(
                session_id="s1",
                turn_id="t1",
                channel="console",
            )
        }
        self.assertEqual(names, {"tool_search", "filesystem"})
        self.assertNotIn("input_schema", host.catalog.get("filesystem").__slots__)

    async def test_search_expands_monotonically_and_returns_activated_schemas(self):
        host = _host()
        await host.begin_turn(
            "прочитай файл",
            session_id="s1",
            turn_id="t1",
            channel="telegram",
        )
        result = await host.search(
            "найди новости в интернете",
            session_id="s1",
            turn_id="t1",
            channel="telegram",
            top_k=1,
        )

        self.assertEqual(result["activated"], ["web.search"])
        self.assertNotIn("input_schema", result["matches"][0])
        self.assertEqual(result["tools"][0]["id"], "web.search")
        self.assertIn("input_schema", result["tools"][0])
        turn = host.current_turn(session_id="s1", channel="telegram")
        self.assertEqual(
            turn.active_ids,
            [TOOL_SEARCH_ID, "filesystem", "web.search"],
        )

    async def test_search_rescues_low_embedding_score_with_lexical_evidence(self):
        class LowScoreEmbeddings(FakeEmbeddings):
            async def embed(self, texts, *, input_type):
                if input_type == "query":
                    return [(1.0, 0.0, 0.0) for _ in texts]
                return [
                    (0.15, 0.99, 0.0)
                    if "Tool: web.search" in text
                    else (0.0, 1.0, 0.0)
                    for text in texts
                ]

        host = _host(LowScoreEmbeddings())
        await host.begin_turn(
            "привет",
            session_id="s1",
            turn_id="t1",
            channel="console",
        )
        result = await host.search(
            "последние новости Украина",
            session_id="s1",
            turn_id="t1",
            channel="console",
        )

        self.assertEqual(result["activated"], ["web.search"])
        self.assertEqual(host.router.last_route["fallback"], "lexical")

    async def test_search_tells_model_when_no_tool_matches(self):
        class NoMatchEmbeddings(FakeEmbeddings):
            async def embed(self, texts, *, input_type):
                if input_type == "query":
                    return [(0.0, 0.0, 0.0) for _ in texts]
                return await super().embed(texts, input_type=input_type)

        host = _host(NoMatchEmbeddings())
        await host.begin_turn(
            "launch a browser",
            session_id="s1",
            turn_id="t1",
            channel="console",
        )

        result = await host.search(
            "open a desktop browser",
            session_id="s1",
            turn_id="t1",
            channel="console",
        )

        self.assertFalse(result["found"])
        self.assertEqual(result["activated"], [])
        self.assertEqual(result["matches"], [])
        self.assertIn("Tell the user", result["message"])

    async def test_new_turn_drops_previous_expansion_and_reuses_index(self):
        client = FakeEmbeddings()
        host = _host(client)
        await host.begin_turn(
            "прочитай файл",
            session_id="s1",
            turn_id="t1",
            channel="console",
        )
        await host.search(
            "найди новости",
            session_id="s1",
            turn_id="t1",
            channel="console",
        )
        await host.begin_turn(
            "прочитай другой файл",
            session_id="s1",
            turn_id="t2",
            channel="console",
        )

        turn = host.current_turn(session_id="s1", channel="console")
        self.assertEqual(turn.active_ids, [TOOL_SEARCH_ID, "filesystem"])
        document_calls = [call for call in client.calls if call[0] == "document"]
        self.assertEqual(len(document_calls), 1)

    async def test_embedding_failure_is_lexical_and_never_full_catalog(self):
        host = _host(FakeEmbeddings(fail=True))
        turn = await host.begin_turn(
            "unrelated question",
            session_id="s1",
            turn_id="t1",
            channel="console",
        )
        self.assertEqual(turn.active_ids, [TOOL_SEARCH_ID])
        self.assertTrue(host.router.last_route["fallback"])

    async def test_anti_example_vetoes_false_positive(self):
        client = FakeEmbeddings()
        config = ToolRoutingConfig(
            dimension=3,
            top_k=2,
            min_similarity=0.2,
        )
        host = ToolRoutingHost(config, client=client)
        host.sync(
            [
                (
                    "scheduler",
                    _tool(
                        "scheduler",
                        summary="Schedule automated jobs with cron",
                        intents=["create schedules"],
                        examples=["schedule a nightly backup"],
                        anti_examples=["explain cron syntax"],
                    ),
                )
            ]
        )
        turn = await host.begin_turn(
            "explain cron syntax",
            session_id="s1",
            turn_id="t1",
            channel="console",
        )
        self.assertEqual(turn.active_ids, [TOOL_SEARCH_ID])

    async def test_channel_and_policy_prefilter(self):
        host = ToolRoutingHost(
            ToolRoutingConfig(dimension=3, top_k=3),
            client=FakeEmbeddings(),
        )
        telegram_only = _tool(
            "telegram.only",
            summary="Search web news",
            intents=["news"],
            examples=["find news"],
            channels=["telegram"],
        )
        denied = _tool(
            "denied",
            summary="Search web news",
            intents=["news"],
            examples=["find news"],
        )
        host.sync([("telegram.only", telegram_only), ("denied", denied)])
        policy = SimpleNamespace(
            deny_capabilities={"denied"},
            deny_scopes=set(),
            deny_effects=set(),
        )
        turn = await host.begin_turn(
            "find web news",
            session_id="s1",
            turn_id="t1",
            channel="console",
            policy=policy,
        )
        self.assertEqual(turn.active_ids, [TOOL_SEARCH_ID])

    async def test_inactive_tool_call_is_rejected(self):
        host = _host()
        await host.begin_turn(
            "прочитай файл",
            session_id="s1",
            turn_id="t1",
            channel="console",
        )
        with self.assertRaises(PermissionError):
            host.require_active(
                "web.search",
                session_id="s1",
                turn_id="t1",
                channel="console",
            )

    def test_colliding_model_names_are_unique(self):
        host = ToolRoutingHost(
            ToolRoutingConfig(dimension=3),
            client=FakeEmbeddings(),
        )
        host.sync(
            [
                ("foo.bar", _tool("foo.bar", summary="one", intents=[], examples=[])),
                ("foo_bar", _tool("foo_bar", summary="two", intents=[], examples=[])),
            ]
        )
        names = [item["model_name"] for item in host.all_specs()]
        self.assertEqual(len(names), len(set(names)))

    def test_model_schema_prefix_is_stable(self):
        host = _host()
        names = [
            item["function"]["name"] for item in host.model_schemas()
        ]
        self.assertEqual(names, ["tool_search", "tool_call"])
        self.assertEqual(
            {item["id"] for item in host.all_specs()},
            {TOOL_SEARCH_ID, TOOL_CALL_ID, "filesystem", "web.search"},
        )

    def test_embedding_passage_contains_only_semantic_metadata(self):
        passage = _host().catalog.get("web.search").routing_text
        self.assertIn("Summary: Search current information on the web", passage)
        self.assertIn("Intents: internet research and current news", passage)
        for label in (
            "Permission:",
            "Risk:",
            "Side effects:",
            "Required scopes:",
            "Channels:",
            "Cost:",
        ):
            self.assertNotIn(label, passage)

    def test_trivial_filter_is_conservative(self):
        for text in ("привет", "thanks!", "👍", "ok"):
            self.assertTrue(is_trivial_chitchat(text), text)
        for text in ("прочитай файл", "latest news", "объясни cron"):
            self.assertFalse(is_trivial_chitchat(text), text)


if __name__ == "__main__":
    unittest.main()
