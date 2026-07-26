"""Prompt file loading for chat mode."""

from __future__ import annotations

import asyncio
import signal
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from main import (
    _chat_system_prompt,
    _minimal_runtime_snapshot,
    _request_shutdown,
    _run,
    _needs_setup,
    _resolve_command,
    build_parser,
)


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

    def test_packaged_policy_requires_live_sources_for_current_events(self) -> None:
        prompt = _chat_system_prompt(Path(__file__).resolve().parents[1])

        self.assertIn("Current And Latest Information", prompt or "")
        self.assertIn("require an available web-search", prompt or "")
        self.assertIn("web.fetch", prompt or "")
        self.assertIn("source URLs", prompt or "")
        self.assertIn("Never guess current facts", prompt or "")


class CliCommandTests(unittest.TestCase):
    def test_default_command_is_inline_tui(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(_resolve_command(args), "tui")

    def test_explicit_tui_command(self) -> None:
        args = build_parser().parse_args(["tui"])
        self.assertEqual(_resolve_command(args), "tui")

    def test_explicit_setup_command(self) -> None:
        args = build_parser().parse_args(["setup"])
        self.assertEqual(_resolve_command(args), "setup")

    def test_gateway_subcommand(self) -> None:
        args = build_parser().parse_args(["gateway"])
        self.assertEqual(_resolve_command(args), "gateway")

    def test_security_subcommand_keeps_control_arguments(self) -> None:
        args = build_parser().parse_args(["security", "mode", "auto"])
        self.assertEqual(_resolve_command(args), "security")
        self.assertEqual(args.command_args, ["mode", "auto"])

    def test_legacy_chat_flag_is_gateway(self) -> None:
        args = build_parser().parse_args(["--chat"])
        self.assertEqual(_resolve_command(args), "gateway")

    def test_menu_alias_is_advanced_settings(self) -> None:
        args = build_parser().parse_args(["menu"])
        self.assertEqual(_resolve_command(args), "settings")

    def test_first_run_gates_interactive_entrypoints_only(self) -> None:
        self.assertTrue(_needs_setup("tui", True))
        self.assertTrue(_needs_setup("chat", True))
        self.assertTrue(_needs_setup("gateway", True))
        self.assertTrue(_needs_setup("setup", False))
        self.assertFalse(_needs_setup("doctor", True))
        self.assertFalse(_needs_setup("status", True))


class SignalTests(unittest.TestCase):
    def test_snapshot_works_without_resource_module(self) -> None:
        app = SimpleNamespace(runtime=None)
        with (
            patch("main.resource", None),
            patch("main.os.getpid", return_value=4321),
        ):
            self.assertEqual(
                _minimal_runtime_snapshot(app),
                {"pid": 4321, "rss_bytes": None, "running": False},
            )

    def test_snapshot_includes_live_chat_diagnostics(self) -> None:
        status = SimpleNamespace(
            running=True,
            uptime_seconds=1.25,
            mode="assistant",
        )
        app = SimpleNamespace(
            runtime=SimpleNamespace(
                running=True,
                snapshot=lambda: status,
            ),
            _chat_diagnostic_snapshot=lambda: {
                "active_turn": True,
                "reasoning_chars": 42,
                "last_provider_prompt_tokens": 2201,
                "pending_operation": "model.stream",
            },
        )
        with (
            patch("main.resource", None),
            patch("main.os.getpid", return_value=4321),
        ):
            snapshot = _minimal_runtime_snapshot(app)

        self.assertEqual(
            snapshot["chat"],
            {
                "active_turn": True,
                "reasoning_chars": 42,
                "last_provider_prompt_tokens": 2201,
                "pending_operation": "model.stream",
            },
        )

    def test_shutdown_signals_log_name_snapshot_and_cancel_host_task(self) -> None:
        status = SimpleNamespace(
            running=True,
            uptime_seconds=12.3456,
            mode="assistant",
        )
        snapshot = {
            "pid": 4321,
            "rss_bytes": 8192,
            "running": True,
            "uptime_seconds": 12.346,
            "mode": "assistant",
        }
        for signum, name, platform, raw_rss in (
            (signal.SIGINT, "SIGINT", "darwin", 8192),
            (signal.SIGTERM, "SIGTERM", "linux", 8),
        ):
            with self.subTest(signal=name):
                logger = Mock()
                task = Mock()
                app = SimpleNamespace(
                    log=logger,
                    runtime=SimpleNamespace(
                        running=True,
                        snapshot=lambda: status,
                    ),
                )
                state = {"signum": 0}

                with (
                    patch("main.os.getpid", return_value=4321),
                    patch(
                        "main.resource.getrusage",
                        return_value=SimpleNamespace(ru_maxrss=raw_rss),
                    ),
                    patch("main.sys.platform", platform),
                ):
                    _request_shutdown(app, task, state, signum)

                self.assertEqual(state["signum"], signum)
                task.cancel.assert_called_once_with()
                logger.warning.assert_called_once_with(
                    "shutdown requested: signal=%s runtime=%s",
                    name,
                    snapshot,
                )

    def test_signal_cancellation_returns_shell_code_after_shutdown(self) -> None:
        class Runtime:
            async def status(self):
                await asyncio.Future()

        class App:
            instance = None

            def __init__(self, _config_path) -> None:
                App.instance = self
                self.runtime = Runtime()
                self.shutdown_called = False

            async def boot(self) -> None:
                return None

            async def shutdown(self) -> None:
                self.shutdown_called = True

        def install(_app):
            asyncio.get_running_loop().call_soon(
                asyncio.current_task().cancel
            )
            return {"signum": signal.SIGTERM}, {}

        args = build_parser().parse_args(["status"])
        with (
            patch("main.CoraxApp", App),
            patch(
                "main._install_shutdown_signal_handlers",
                side_effect=install,
            ),
            patch("main._restore_signal_handlers"),
        ):
            result = asyncio.run(_run(args))

        self.assertEqual(result, 143)
        self.assertTrue(App.instance.shutdown_called)


if __name__ == "__main__":
    unittest.main()
