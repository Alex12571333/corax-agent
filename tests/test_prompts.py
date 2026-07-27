"""Prompt file loading for chat mode."""

from __future__ import annotations

import asyncio
import signal
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from corax.cli import (
    _chat_system_prompt,
    _minimal_runtime_snapshot,
    _needs_setup,
    _parse_confirmation_command,
    _request_shutdown,
    _resolve_command,
    _run,
    _run_setup_wizard,
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

    def test_packaged_prompt_preserves_conversation_continuity(self) -> None:
        prompt = _chat_system_prompt(Path(__file__).resolve().parents[1])

        self.assertIn("Conversation Continuity", prompt or "")
        self.assertIn("not a separate session", prompt or "")
        self.assertIn("do not greet again", prompt or "")


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

    def test_confirmation_command_scopes(self) -> None:
        self.assertEqual(
            _parse_confirmation_command("approve task-1"),
            ("task-1", "allow_once"),
        )
        self.assertEqual(
            _parse_confirmation_command("approve task-1 turn"),
            ("task-1", "allow_turn"),
        )
        self.assertEqual(
            _parse_confirmation_command("approve task-1 session"),
            ("task-1", "allow_session"),
        )
        self.assertEqual(
            _parse_confirmation_command("deny task-1 rule"),
            ("task-1", "deny_rule"),
        )
        task_id, error = _parse_confirmation_command("approve task-1 forever")
        self.assertIsNone(task_id)
        self.assertIn("once|turn|session", error)

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

    def test_missing_console_marks_guided_setup_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(sys.modules, {"corax_console": None}):
                result = asyncio.run(
                    _run_setup_wizard(
                        Path(tmp) / "corax.json",
                        first_run=True,
                    )
                )

        self.assertIsNone(result)

    def test_eval_uses_installed_source_root(self) -> None:
        report = SimpleNamespace(ok=True, render=Mock(return_value="ok"))
        run_evaluations = Mock(return_value=report)
        app = SimpleNamespace(
            runtime=SimpleNamespace(root_path=Path("/wrong/runtime/path")),
            boot=AsyncMock(),
            shutdown=AsyncMock(),
        )
        with (
            patch("corax.cli._needs_setup", return_value=False),
            patch("corax.cli.CoraxApp", return_value=app),
            patch("corax.cli._print_result"),
            patch.dict(
                sys.modules,
                {
                    "corax_evals": SimpleNamespace(
                        run_evaluations=run_evaluations,
                    )
                },
            ),
        ):
            result = asyncio.run(_run(build_parser().parse_args(["eval"])))

        agent_root = Path(sys.modules["corax.cli"].__file__).resolve().parents[1]
        self.assertEqual(result, 0)
        run_evaluations.assert_called_once_with(agent_root, agent_root.parent)

    def test_setup_gated_commands_fall_back_to_builtin_menu(self) -> None:
        for argv in ([], ["setup"], ["gateway"]):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as tmp:
                app = SimpleNamespace(
                    boot=AsyncMock(),
                    shutdown=AsyncMock(),
                    run_menu=AsyncMock(return_value="eof"),
                )
                with (
                    patch(
                        "corax.cli._resolve_config_path",
                        return_value=Path(tmp) / "corax.json",
                    ),
                    patch(
                        "corax.cli._run_setup_wizard",
                        new=AsyncMock(return_value=None),
                    ),
                    patch("corax.cli.CoraxApp", return_value=app),
                    patch("corax.cli._print_setup_overview"),
                ):
                    result = asyncio.run(_run(build_parser().parse_args(argv)))

                self.assertEqual(result, 0)
                app.run_menu.assert_awaited_once()
                app.shutdown.assert_awaited_once()

    def test_tui_and_chat_fall_back_when_console_is_incomplete(self) -> None:
        cases = (
            ("tui", False, "model"),
            ("chat", True, ""),
        )
        for command, has_console, model_id in cases:
            with self.subTest(command=command):
                runtime = SimpleNamespace(
                    channels=SimpleNamespace(
                        has=MagicMock(return_value=has_console),
                    ),
                    active_generation_model_id=MagicMock(
                        return_value=model_id,
                    ),
                )
                app = SimpleNamespace(
                    runtime=runtime,
                    boot=AsyncMock(),
                    shutdown=AsyncMock(),
                    run_menu=AsyncMock(return_value="eof"),
                )
                run_console = AsyncMock()
                with (
                    patch("corax.cli._needs_setup", return_value=False),
                    patch("corax.cli.CoraxApp", return_value=app),
                    patch("corax.cli._run_console_chat", new=run_console),
                ):
                    result = asyncio.run(
                        _run(build_parser().parse_args([command]))
                    )

                self.assertEqual(result, 0)
                app.run_menu.assert_awaited_once()
                run_console.assert_not_awaited()


class SignalTests(unittest.TestCase):
    def test_snapshot_works_without_resource_module(self) -> None:
        app = SimpleNamespace(runtime=None)
        with (
            patch("corax.cli.resource", None),
            patch("corax.cli.os.getpid", return_value=4321),
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
            patch("corax.cli.resource", None),
            patch("corax.cli.os.getpid", return_value=4321),
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
                    patch("corax.cli.os.getpid", return_value=4321),
                    patch(
                        "corax.cli.resource.getrusage",
                        return_value=SimpleNamespace(ru_maxrss=raw_rss),
                    ),
                    patch("corax.cli.sys.platform", platform),
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
            patch("corax.cli.CoraxApp", App),
            patch(
                "corax.cli._install_shutdown_signal_handlers",
                side_effect=install,
            ),
            patch("corax.cli._restore_signal_handlers"),
        ):
            result = asyncio.run(_run(args))

        self.assertEqual(result, 143)
        self.assertTrue(App.instance.shutdown_called)


if __name__ == "__main__":
    unittest.main()
