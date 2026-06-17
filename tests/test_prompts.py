"""Prompt file loading for chat mode."""

from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from main import _chat_system_prompt, _resolve_command, build_parser


class ChatPromptTests(unittest.TestCase):
    def test_chat_system_prompt_loads_system_and_safety_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_dir = root / "prompts"
            prompt_dir.mkdir()
            (prompt_dir / "system.md").write_text("system rules\n", encoding="utf-8")
            (prompt_dir / "safety.md").write_text("safety rules\n", encoding="utf-8")

            prompt = _chat_system_prompt(root)

        self.assertIsNotNone(prompt)
        self.assertIn("system rules", prompt or "")
        self.assertIn("safety rules", prompt or "")

    def test_chat_system_prompt_returns_none_without_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_chat_system_prompt(Path(tmp)))


class CliCommandTests(unittest.TestCase):
    def test_default_command_is_setup(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(_resolve_command(args), "setup")

    def test_gateway_subcommand(self) -> None:
        args = build_parser().parse_args(["gateway"])
        self.assertEqual(_resolve_command(args), "gateway")

    def test_legacy_chat_flag_is_gateway(self) -> None:
        args = build_parser().parse_args(["--chat"])
        self.assertEqual(_resolve_command(args), "gateway")

    def test_menu_alias_is_setup(self) -> None:
        args = build_parser().parse_args(["menu"])
        self.assertEqual(_resolve_command(args), "setup")


if __name__ == "__main__":
    unittest.main()
