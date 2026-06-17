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


if __name__ == "__main__":
    unittest.main()
