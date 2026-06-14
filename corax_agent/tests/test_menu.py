"""Menu & app: scripted input never crashes, edits apply, paths are created."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from corax_agent import config as cfg
from corax_agent.app import CoraxApp
from corax_agent.menu import Menu
from corax_agent.runtime import CoraxRuntime
from corax_agent.tests._helpers import scripted_terminal


def _menu(inputs, config=None, config_path="agent.json", runtime=None, save_fn=None):
    config = config or cfg.default_config()
    term, capture = scripted_terminal(inputs)
    menu = Menu(
        config=config,
        config_path=Path(config_path),
        runtime=runtime,
        terminal=term,
        save_fn=save_fn,
    )
    return menu, capture


class TestMenuFlows(unittest.TestCase):
    def test_exit_without_saving(self) -> None:
        menu, _ = _menu(["0"])
        self.assertEqual(menu.run(), "discarded")
        self.assertFalse(menu.changed)

    def test_eof_returns_eof(self) -> None:
        menu, _ = _menu([])  # no input at all
        self.assertEqual(menu.run(), "eof")

    def test_edit_runtime_log_level(self) -> None:
        menu, _ = _menu(["2", "1", "DEBUG", "", "0"])
        self.assertEqual(menu.run(), "discarded")
        self.assertEqual(menu.config.runtime.log_level, "DEBUG")
        self.assertTrue(menu.changed)

    def test_toggle_security_flag(self) -> None:
        menu, _ = _menu(["7", "3", "", "0"])  # security -> toggle allow_shell -> back
        menu.run()
        self.assertTrue(menu.config.security.allow_shell)

    def test_deactivate_connector(self) -> None:
        menu, _ = _menu(["5", "x", "terminal", "", "0"])
        menu.run()
        self.assertNotIn("terminal", menu.config.connectors.active)
        self.assertTrue(menu.changed)

    def test_edit_limit_int(self) -> None:
        menu, _ = _menu(["8", "1", "9", "", "0"])
        menu.run()
        self.assertEqual(menu.config.limits.max_parallel_tasks, 9)

    def test_unknown_option_does_not_crash(self) -> None:
        menu, capture = _menu(["zzz", "0"])
        self.assertEqual(menu.run(), "discarded")
        self.assertIn("unknown option", capture.text)

    def test_save_calls_save_fn_and_clears_first_run(self) -> None:
        saved = {}

        def save_fn(config):
            saved["called"] = True
            saved["first_run"] = config.agent.first_run

        menu, _ = _menu(["10"], save_fn=save_fn)
        self.assertEqual(menu.run(), "saved")
        self.assertTrue(saved["called"])
        self.assertFalse(saved["first_run"])

    def test_status_screen_with_runtime(self) -> None:
        config = cfg.default_config()
        runtime = CoraxRuntime(config)
        asyncio.run(runtime.start())
        try:
            menu, capture = _menu(["1", "0"], config=config, runtime=runtime)
            self.assertEqual(menu.run(), "discarded")
            self.assertIn("RUNNING", capture.text)
        finally:
            asyncio.run(runtime.stop())

    def test_paths_screen(self) -> None:
        menu, capture = _menu(["9", "0"])
        menu.run()
        self.assertIn("workspace", capture.text)


class TestAppLifecycle(unittest.TestCase):
    def test_boot_creates_config_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "agent.json"
            app = CoraxApp(config_path)
            asyncio.run(app.boot())
            try:
                self.assertTrue(config_path.exists())
                self.assertTrue(app.paths.workspace.is_dir())
                self.assertTrue(app.paths.data.is_dir())
                self.assertTrue(app.paths.logs.is_dir())
                self.assertTrue(app.runtime.running)
                status = asyncio.run(app.runtime.status())
                self.assertEqual(status.planner_active, "stub")
            finally:
                asyncio.run(app.shutdown())

    def test_boot_then_menu_then_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "agent.json"
            term, _ = scripted_terminal(["2", "1", "WARNING", "", "10"])
            app = CoraxApp(config_path, terminal=term)
            asyncio.run(app.boot())
            result = asyncio.run(app.run_menu())
            asyncio.run(app.shutdown())
            self.assertEqual(result, "saved")
            reloaded = cfg.load_config(config_path)
            self.assertEqual(reloaded.runtime.log_level, "WARNING")
            self.assertFalse(reloaded.agent.first_run)


if __name__ == "__main__":
    unittest.main()
