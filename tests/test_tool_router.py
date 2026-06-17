"""LLM tool router: parsing, fallback, and robustness."""

from __future__ import annotations

import unittest

from corax.tool_router import LLMToolRouter

CATALOG = [
    {
        "id": "filesystem",
        "description": "Read, write, list and delete workspace files",
        "input_schema": {
            "type": "object",
            "properties": {"operation": {"enum": ["list", "read", "write", "delete"]}},
        },
    },
    {"id": "shell", "description": "Run shell commands", "input_schema": {}},
    {"id": "web.search", "description": "Search the public web via SearXNG", "input_schema": {}},
]


def _runner(text):
    async def _run(_cap_id, _payload):
        return {"text": text}

    return _run


def _boom(_cap_id, _payload):
    raise RuntimeError("llm down")


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_json_array(self) -> None:
        router = LLMToolRouter(_runner('["filesystem"]'), catalog=CATALOG)
        self.assertEqual(await router.route("удали файл x", []), ["filesystem"])

    async def test_composite_intent(self) -> None:
        router = LLMToolRouter(
            _runner('Sure: ["web.search", "filesystem"]'), catalog=CATALOG
        )
        self.assertEqual(
            await router.route("узнай погоду и сохрани в файл", []),
            ["web.search", "filesystem"],
        )

    async def test_empty_array_is_authoritative_no_fallback(self) -> None:
        # Model says "no tools needed" -> [] (not a fallback trigger).
        called = {"n": 0}

        def fallback(_q, _s):
            called["n"] += 1
            return ["filesystem"]

        router = LLMToolRouter(_runner("[]"), catalog=CATALOG, fallback=fallback)
        self.assertEqual(await router.route("привет", []), [])
        self.assertEqual(called["n"], 0)

    async def test_normalises_safe_names(self) -> None:
        router = LLMToolRouter(_runner('["web_search"]'), catalog=CATALOG)
        self.assertEqual(await router.route("новости", []), ["web.search"])

    async def test_drops_unknown_ids(self) -> None:
        router = LLMToolRouter(_runner('["filesystem", "rm -rf", 5]'), catalog=CATALOG)
        self.assertEqual(await router.route("x", []), ["filesystem"])

    async def test_unparseable_reply_uses_fallback(self) -> None:
        router = LLMToolRouter(
            _runner("I think filesystem"),
            catalog=CATALOG,
            fallback=lambda _q, _s: ["filesystem"],
        )
        self.assertEqual(await router.route("list files", []), ["filesystem"])

    async def test_llm_error_uses_fallback(self) -> None:
        router = LLMToolRouter(
            _boom, catalog=CATALOG, fallback=lambda _q, _s: ["shell"]
        )
        self.assertEqual(await router.route("run tests", []), ["shell"])

    async def test_error_without_fallback_returns_empty(self) -> None:
        router = LLMToolRouter(_boom, catalog=CATALOG)
        self.assertEqual(await router.route("x", []), [])

    async def test_fallback_filters_unknown_and_is_capped(self) -> None:
        router = LLMToolRouter(
            _runner("garbage"),
            catalog=CATALOG,
            top_k=1,
            fallback=lambda _q, _s: ["web.search", "nope", "shell"],
        )
        self.assertEqual(await router.route("x", []), ["web.search"])

    async def test_top_k_caps_llm_selection(self) -> None:
        router = LLMToolRouter(
            _runner('["filesystem", "shell", "web.search"]'), catalog=CATALOG, top_k=2
        )
        self.assertEqual(len(await router.route("do everything", [])), 2)

    async def test_non_string_text_uses_fallback(self) -> None:
        async def _run(_cap_id, _payload):
            return {"text": None}

        router = LLMToolRouter(_run, catalog=CATALOG, fallback=lambda _q, _s: ["shell"])
        self.assertEqual(await router.route("x", []), ["shell"])

    async def test_menu_lists_operations(self) -> None:
        router = LLMToolRouter(_runner("[]"), catalog=CATALOG)
        self.assertIn("filesystem", router._menu)
        self.assertIn("operations: list, read, write, delete", router._menu)
        self.assertIn("web.search", router._menu)


if __name__ == "__main__":
    unittest.main()
