"""Embedding-first tool catalog, selection and per-turn activation."""

from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace

from corax.config import ToolRoutingConfig
from corax.tool_router import (
    OBJECT_RUN_ID,
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
    output_schema: dict | None = None,
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
        output_schema=output_schema or {},
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
        self.assertEqual(
            host.object_model_schema()[0]["function"]["name"],
            "object_run",
        )
        self.assertIn(
            OBJECT_RUN_ID,
            {spec["id"] for spec in host.all_specs()},
        )

    async def test_object_facade_keeps_capability_ids_in_host_mapping(self):
        host = _host()
        await host.begin_turn(
            "прочитай файл config.yaml",
            session_id="s1",
            turn_id="t1",
            channel="console",
        )

        facade, mapping = host.object_facade(
            session_id="s1",
            turn_id="t1",
            channel="console",
            publish=True,
        )

        self.assertIn("run(value: str | None) -> dict", facade["files"])
        self.assertNotIn("filesystem", str(facade))
        self.assertEqual(mapping["files.run"]["capability"], "filesystem")
        self.assertEqual(
            host.resolve_object_method(
                "files.run",
                {"value": "x"},
                session_id="s1",
                turn_id="t1",
                channel="console",
            ),
            ("filesystem", {"operation": "run", "value": "x"}),
        )
        self.assertEqual(
            host.resolve_object_method(
                "files.run",
                {"value": None},
                session_id="s1",
                turn_id="t1",
                channel="console",
            ),
            ("filesystem", {"operation": "run"}),
        )
        self.assertEqual(
            host.resolve_object_method(
                "tools.search",
                {"query": "files", "top_k": None},
                session_id="s1",
                turn_id="t1",
                channel="console",
            ),
            (TOOL_SEARCH_ID, {"query": "files"}),
        )
        with self.assertRaises(ValueError):
            host.resolve_object_method(
                "files.run",
                {"operation": "delete"},
                session_id="s1",
                turn_id="t1",
                channel="console",
            )

    async def test_object_facade_exposes_bounded_output_fields_only(self):
        host = ToolRoutingHost(
            ToolRoutingConfig(dimension=3),
            client=FakeEmbeddings(),
        )
        host.sync(
            [
                (
                    "shell",
                    _tool(
                        "shell",
                        summary="Run a command",
                        intents=["run command"],
                        examples=["run tests"],
                        output_schema={
                            "type": "object",
                            "properties": {
                                "operation": {"type": "string"},
                                "requested_url": {"type": "string"},
                                "final_url": {"type": "string"},
                                "redirects": {"type": "array"},
                                "title": {"type": "string"},
                                "text": {"type": "string"},
                                "published_at": {},
                                "source": {"type": "string"},
                                "content_type": {"type": "string"},
                                "retrieved_at": {"type": "string"},
                                "bytes_read": {"type": "integer"},
                                "truncated": {"type": "boolean"},
                                "optional": {"type": "boolean"},
                                "bad\nfield": {
                                    "type": "string",
                                    "description": "INJECT",
                                },
                            },
                            "required": [
                                "operation",
                                "requested_url",
                                "final_url",
                                "redirects",
                                "title",
                                "text",
                                "published_at",
                                "source",
                                "content_type",
                                "retrieved_at",
                                "bytes_read",
                                "truncated",
                            ],
                            "description": "SECRET FULL OUTPUT SCHEMA",
                        },
                    ),
                )
            ]
        )
        await host.begin_turn(
            "run tests",
            session_id="contracts",
            turn_id="contracts-1",
            channel="console",
        )

        facade, mapping = host.object_facade(
            session_id="contracts",
            turn_id="contracts-1",
            channel="console",
        )
        rendered = str(facade)

        self.assertIn('"title": str', rendered)
        self.assertIn('"text": str', rendered)
        self.assertIn('"source": str', rendered)
        self.assertIn('"truncated": bool', rendered)
        self.assertNotIn('"optional"?: bool', rendered)
        self.assertNotIn("bad", rendered)
        self.assertNotIn("INJECT", rendered)
        self.assertNotIn("SECRET FULL OUTPUT SCHEMA", rendered)
        self.assertNotIn('"properties"', rendered)
        self.assertEqual(mapping["shell.run"]["capability"], "shell")

    async def test_object_facade_preserves_mcp_server_namespace(self):
        host = _host()
        host.sync(
            [
                (
                    "mcp.github.search",
                    _tool(
                        "mcp.github.search",
                        summary="Search GitHub",
                        intents=["search github"],
                        examples=["search repo"],
                    ),
                ),
                (
                    "mcp.docs.search",
                    _tool(
                        "mcp.docs.search",
                        summary="Search docs",
                        intents=["search docs"],
                        examples=["search docs"],
                    ),
                ),
            ]
        )
        await host.begin_turn(
            "search github and docs",
            session_id="mcp",
            turn_id="mcp-1",
            channel="console",
        )
        turn = host.current_turn(session_id="mcp", channel="console")
        turn.activate(
            ["mcp.github.search", "mcp.docs.search"],
            host.schemas,
            max_tools=4,
            max_schema_bytes=20_000,
        )

        facade, mapping = host.object_facade(
            session_id="mcp",
            turn_id="mcp-1",
            channel="console",
        )

        self.assertIn("mcp_github", facade)
        self.assertIn("mcp_docs", facade)
        self.assertEqual(
            mapping["mcp_github.run"]["capability"],
            "mcp.github.search",
        )
        self.assertEqual(
            mapping["mcp_docs.run"]["capability"],
            "mcp.docs.search",
        )

    async def test_object_method_bindings_do_not_change_when_turn_expands(self):
        host = _host()
        host.sync(
            [
                (
                    "filesystem",
                    _tool(
                        "filesystem",
                        summary="Read workspace files",
                        intents=["local file operations"],
                        examples=["read file"],
                    ),
                ),
                (
                    "editor",
                    _tool(
                        "editor",
                        summary="Edit source code",
                        intents=["modify code"],
                        examples=["replace code"],
                    ),
                ),
            ]
        )
        turn = await host.begin_turn(
            "read a local file",
            session_id="stable",
            turn_id="stable-1",
            channel="console",
        )
        _, before = host.object_facade(
            session_id="stable",
            turn_id="stable-1",
            channel="console",
        )
        self.assertEqual(before["files.run"]["capability"], "filesystem")

        turn.activate(
            ["editor"],
            host.schemas,
            max_tools=4,
            max_schema_bytes=20_000,
        )
        facade, after = host.object_facade(
            session_id="stable",
            turn_id="stable-1",
            channel="console",
        )
        self.assertEqual(after["files.run"]["capability"], "filesystem")
        editor_alias = next(
            alias
            for alias, descriptor in after.items()
            if descriptor["capability"] == "editor"
        )
        self.assertRegex(editor_alias, r"^files\.run_[0-9a-f]{8}$")
        self.assertIn(editor_alias.split(".", 1)[1] + "(value:", str(facade))

    async def test_object_method_hash_cannot_collide_with_literal_method(self):
        host = _host()
        collision = "run_" + hashlib.sha256(b"web.b\0run").hexdigest()[:8]
        tools = [
            _tool(
                extension_id,
                summary=extension_id,
                intents=[extension_id],
                examples=[extension_id],
            )
            for extension_id in ("web.a", "web.b", "web.c")
        ]
        tools[2].input_schema["properties"]["operation"]["enum"] = [collision]
        host.sync([(tool.id, tool) for tool in tools])
        turn = await host.begin_turn(
            "web a",
            session_id="collision",
            turn_id="collision-1",
            channel="console",
        )
        turn.activate(
            [tool.id for tool in tools],
            host.schemas,
            max_tools=8,
            max_schema_bytes=20_000,
        )
        _, mapping = host.object_facade(
            session_id="collision",
            turn_id="collision-1",
            channel="console",
        )
        aliases = [
            alias
            for alias, descriptor in mapping.items()
            if descriptor["capability"].startswith("web.")
        ]
        self.assertEqual(len(aliases), 3)
        self.assertEqual(len(set(aliases)), 3)

    async def test_object_discovery_method_cannot_be_shadowed(self):
        host = ToolRoutingHost(
            ToolRoutingConfig(dimension=3),
            client=FakeEmbeddings(),
        )
        plugin = _tool(
            "search",
            summary="Plugin search",
            intents=["search"],
            examples=["search"],
        )
        plugin.input_schema["properties"].pop("operation")
        host.sync([("search", plugin)])
        turn = await host.begin_turn(
            "search",
            session_id="reserved",
            turn_id="reserved-1",
            channel="console",
        )
        turn.activate(
            ["search"],
            host.schemas,
            max_tools=4,
            max_schema_bytes=20_000,
        )

        facade, mapping = host.object_facade(
            session_id="reserved",
            turn_id="reserved-1",
            channel="console",
        )

        self.assertEqual(
            mapping["tools.search"]["capability"],
            TOOL_SEARCH_ID,
        )
        plugin_alias = next(
            alias
            for alias, descriptor in mapping.items()
            if descriptor["capability"] == "search"
        )
        self.assertRegex(plugin_alias, r"^tools\.search_[0-9a-f]{8}$")
        self.assertIn(plugin_alias.split(".", 1)[1] + "(", str(facade))

    async def test_object_facade_aliases_keywords_and_schema_keys(self):
        host = ToolRoutingHost(
            ToolRoutingConfig(dimension=3),
            client=FakeEmbeddings(),
        )
        plugin = _tool(
            "class.run",
            summary="Keyword and MCP-like argument test",
            intents=["keyword test"],
            examples=["keyword test"],
            output_schema={
                "type": "object",
                "properties": {
                    "maxResults": {"type": "integer"},
                    "html-url": {"type": "string"},
                    "class": {"type": "string"},
                    "unsafe\ninstruction": {"type": "string"},
                },
                "required": ["maxResults"],
            },
        )
        plugin.input_schema = {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["class"]},
                "from": {"type": "string"},
                "maxResults": {"type": "integer"},
                "max-results": {"type": "integer"},
            },
            "required": ["operation", "from"],
        }
        host.sync([("class.run", plugin)])
        turn = await host.begin_turn(
            "keyword test",
            session_id="aliases",
            turn_id="aliases-1",
            channel="console",
        )
        turn.activate(
            ["class.run"],
            host.schemas,
            max_tools=4,
            max_schema_bytes=20_000,
        )

        facade, mapping = host.object_facade(
            session_id="aliases",
            turn_id="aliases-1",
            channel="console",
            publish=True,
        )
        method = next(
            alias
            for alias, descriptor in mapping.items()
            if descriptor["capability"] == "class.run"
        )
        descriptor = mapping[method]
        max_aliases = [
            alias
            for alias, raw_name in descriptor["arguments"].items()
            if raw_name in {"maxResults", "max-results"}
        ]

        self.assertEqual(method, "class_.class_")
        self.assertIn("from_: str", str(facade))
        self.assertIn(
            '# keys: "maxResults": int, "html-url"?: str, "class"?: str, ...',
            str(facade),
        )
        self.assertNotIn("unsafe\ninstruction", str(facade))
        self.assertEqual(len(max_aliases), 2)
        self.assertEqual(len(set(max_aliases)), 2)
        signature = facade["class_"][0].split(" #", 1)[0].rstrip()
        compile(f"async def {signature}:\n    pass\n", "<test>", "exec")
        self.assertEqual(
            host.resolve_object_method(
                method,
                {
                    "from_": "origin",
                    max_aliases[0]: 10,
                    max_aliases[1]: 20,
                },
                session_id="aliases",
                turn_id="aliases-1",
                channel="console",
            ),
            (
                "class.run",
                {
                    "operation": "class",
                    "from": "origin",
                    descriptor["arguments"][max_aliases[0]]: 10,
                    descriptor["arguments"][max_aliases[1]]: 20,
                },
            ),
        )

    async def test_object_facade_preserves_required_operation_without_enum(self):
        host = ToolRoutingHost(
            ToolRoutingConfig(dimension=3),
            client=FakeEmbeddings(),
        )
        plugin = _tool(
            "mcp.raw.invoke",
            summary="Raw MCP operation",
            intents=["raw operation"],
            examples=["raw operation"],
        )
        plugin.input_schema = {
            "type": "object",
            "properties": {
                "operation": {"type": "string"},
                "payload": {"type": "string"},
            },
            "required": ["operation", "payload"],
        }
        host.sync([("mcp.raw.invoke", plugin)])
        turn = await host.begin_turn(
            "raw operation",
            session_id="raw-operation",
            turn_id="raw-operation-1",
            channel="console",
        )
        turn.activate(
            ["mcp.raw.invoke"],
            host.schemas,
            max_tools=4,
            max_schema_bytes=20_000,
        )

        facade, mapping = host.object_facade(
            session_id="raw-operation",
            turn_id="raw-operation-1",
            channel="console",
            publish=True,
        )
        method = next(
            alias
            for alias, descriptor in mapping.items()
            if descriptor["capability"] == "mcp.raw.invoke"
        )

        self.assertIn("operation: str", str(facade))
        self.assertEqual(
            host.resolve_object_method(
                method,
                {"operation": "read", "payload": "item"},
                session_id="raw-operation",
                turn_id="raw-operation-1",
                channel="console",
            ),
            (
                "mcp.raw.invoke",
                {"operation": "read", "payload": "item"},
            ),
        )

    async def test_generated_facade_respects_signature_and_char_budgets(self):
        host = ToolRoutingHost(
            ToolRoutingConfig(dimension=3),
            client=FakeEmbeddings(),
        )
        plugin = _tool(
            "mcp.long.describe",
            summary="Long MCP schema",
            intents=["long schema"],
            examples=["long schema"],
            output_schema={
                "type": "object",
                "properties": {
                    f"result_{index}_{'y' * 48}": {"type": "string"}
                    for index in range(8)
                },
            },
        )
        plugin.input_schema = {
            "type": "object",
            "properties": {
                f"argument_{index}_{'x' * 48}": {"type": "string"}
                for index in range(5)
            },
            "required": [
                f"argument_{index}_{'x' * 48}"
                for index in range(5)
            ],
        }
        host.sync([("mcp.long.describe", plugin)])
        turn = await host.begin_turn(
            "long schema",
            session_id="long-facade",
            turn_id="long-facade-1",
            channel="console",
        )
        turn.activate(
            ["mcp.long.describe"],
            host.schemas,
            max_tools=4,
            max_schema_bytes=20_000,
        )
        facade, mapping = host.object_facade(
            session_id="long-facade",
            turn_id="long-facade-1",
            channel="console",
            max_chars=600,
        )

        self.assertTrue(all(len(item) <= 512 for items in facade.values() for item in items))
        rendered = [
            f"async self.{group}.{signature}"
            for group, signatures in sorted(facade.items())
            for signature in signatures
        ]
        self.assertLessEqual(len("\n".join(rendered)), 600)
        self.assertEqual(set(mapping), {
            "tools.search",
            *(
                key
                for key, descriptor in mapping.items()
                if descriptor["capability"] == "mcp.long.describe"
            ),
        })

    async def test_generated_facade_respects_group_and_method_caps(self):
        host = ToolRoutingHost(
            ToolRoutingConfig(dimension=3),
            client=FakeEmbeddings(),
        )
        plugins = []
        for server in range(20):
            capability_id = f"mcp.server{server}.action"
            plugin = _tool(
                capability_id,
                summary=f"Server {server}",
                intents=[f"server {server}"],
                examples=[f"server {server}"],
            )
            plugin.input_schema = {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [f"op{index}" for index in range(32)],
                    },
                    **{
                        f"argument_{index}_{'x' * 40}": {"type": "string"}
                        for index in range(5)
                    },
                },
                "required": ["operation"],
            }
            plugins.append((capability_id, plugin))
        host.sync(plugins)
        turn = await host.begin_turn(
            "server 0",
            session_id="facade-caps",
            turn_id="facade-caps-1",
            channel="console",
        )
        turn.activate(
            [capability_id for capability_id, _ in plugins],
            host.schemas,
            max_tools=64,
            max_schema_bytes=1_000_000,
        )

        facade, mapping = host.object_facade(
            session_id="facade-caps",
            turn_id="facade-caps-1",
            channel="console",
            max_chars=100_000,
        )

        self.assertLessEqual(len(facade), 16)
        self.assertLessEqual(max(map(len, facade.values())), 32)
        self.assertEqual(sum(map(len, facade.values())), 128)
        self.assertEqual(len(mapping), 128)
        _, published = host.object_facade(
            session_id="facade-caps",
            turn_id="facade-caps-1",
            channel="console",
            max_chars=20_000,
            publish=True,
        )
        _, default_budget = host.object_facade(
            session_id="facade-caps",
            turn_id="facade-caps-1",
            channel="console",
        )
        extra = sorted(set(published) - set(default_budget))
        self.assertTrue(extra)
        self.assertEqual(
            host.resolve_object_method(
                extra[0],
                {},
                session_id="facade-caps",
                turn_id="facade-caps-1",
                channel="console",
            )[0],
            published[extra[0]]["capability"],
        )

    async def test_published_facade_is_fixed_budget_monotonic_snapshot(self):
        host = ToolRoutingHost(
            ToolRoutingConfig(dimension=3),
            client=FakeEmbeddings(),
        )
        plugins = [
            (
                capability_id,
                _tool(
                    capability_id,
                    summary=capability_id,
                    intents=[capability_id],
                    examples=[capability_id],
                ),
            )
            for capability_id in ("mcp.zed.run", "mcp.aaa.run")
        ]
        host.sync(plugins)
        turn = await host.begin_turn(
            "mcp.zed.run",
            session_id="snapshot",
            turn_id="snapshot-1",
            channel="console",
        )
        turn.active_ids = [TOOL_SEARCH_ID, "mcp.zed.run"]
        candidate, _ = host.object_facade(
            session_id="snapshot",
            turn_id="snapshot-1",
            channel="console",
            max_chars=10_000,
        )
        budget = len(
            "\n".join(
                f"async self.{group}.{signature}"
                for group, signatures in candidate.items()
                for signature in signatures
            )
        )
        with self.assertRaises(KeyError):
            host.resolve_object_method(
                "tools.search",
                {"query": "files"},
                session_id="snapshot",
                turn_id="snapshot-1",
                channel="console",
            )
        _, first = host.object_facade(
            session_id="snapshot",
            turn_id="snapshot-1",
            channel="console",
            max_chars=budget,
            publish=True,
        )
        turn.activate(
            ["mcp.aaa.run"],
            host.schemas,
            max_tools=4,
            max_schema_bytes=20_000,
        )
        _, second = host.object_facade(
            session_id="snapshot",
            turn_id="snapshot-1",
            channel="console",
            max_chars=100_000,
            publish=True,
        )

        self.assertEqual(second, first)
        self.assertEqual(
            host.resolve_object_method(
                "mcp_zed.run",
                {},
                session_id="snapshot",
                turn_id="snapshot-1",
                channel="console",
            ),
            ("mcp.zed.run", {"operation": "run"}),
        )
        with self.assertRaises(KeyError):
            host.resolve_object_method(
                "mcp_aaa.run",
                {},
                session_id="snapshot",
                turn_id="snapshot-1",
                channel="console",
            )

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
        self.assertNotIn("tools", result)
        self.assertEqual(result["matches"][0]["id"], "web.search")
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
            {
                TOOL_SEARCH_ID,
                TOOL_CALL_ID,
                OBJECT_RUN_ID,
                "filesystem",
                "web.search",
            },
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
