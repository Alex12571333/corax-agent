"""Runtime adapter for the standalone tool-discovery plugin."""

from __future__ import annotations

from pathlib import Path
import unittest

from corax.config import default_config
from corax.tool_discovery import RuntimeToolSelector


REPO_ROOT = Path(__file__).resolve().parents[1]


class RuntimeToolSelectorTests(unittest.TestCase):
    def test_selects_relevant_manifest_tools_from_configured_packages(self) -> None:
        selector = RuntimeToolSelector(default_config(), root_path=REPO_ROOT)
        self.assertTrue(selector.available)
        selected = selector.select("исправь ошибку в коде и запусти тесты", [])
        self.assertEqual(selected[:3], ["editor", "filesystem", "shell"])
        self.assertNotIn("gateway", selected)
        self.assertNotIn("telegram.connector", selected)

    def test_file_request_selects_filesystem_not_web_search(self) -> None:
        # Regression: a low-risk web-search tool used to be the only survivor of
        # the selector's prefer-safe penalty on unrecognised file phrasings, so
        # the model misrouted file ops (list/delete) to web.search. File intents
        # must select filesystem and never strand the model with only web.search.
        selector = RuntimeToolSelector(default_config(), root_path=REPO_ROOT)
        for query in (
            "покажи список файлов в папке",
            "удали файл weather_mokpo_tomorrow.txt",
            "прочитай файл notes.txt",
            "delete the file report.txt",
        ):
            selected = selector.select(query, [])
            self.assertIn("filesystem", selected, query)
            self.assertNotEqual(selected, ["web.search"], query)

    def test_web_query_selects_web_search(self) -> None:
        selector = RuntimeToolSelector(default_config(), root_path=REPO_ROOT)
        for query in ("какая погода в Мокпо завтра", "найди последние новости"):
            self.assertIn("web.search", selector.select(query, []), query)

    def test_no_tool_intent_returns_empty(self) -> None:
        # No relevance signal -> empty selection. The gateway then offers NO
        # tools for the turn (greetings/small talk shouldn't carry the whole
        # tool catalogue), which is the point of dynamic selection.
        selector = RuntimeToolSelector(default_config(), root_path=REPO_ROOT)
        self.assertEqual(selector.select("привет, как дела", []), [])


if __name__ == "__main__":
    unittest.main()
