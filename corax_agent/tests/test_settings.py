"""Settings: get/set by path, type coercion, provider management."""

from __future__ import annotations

import unittest

from corax_agent import config as cfg
from corax_agent import settings
from corax_agent.settings import SettingError


class TestGetSet(unittest.TestCase):
    def setUp(self) -> None:
        self.config = cfg.default_config()

    def test_get_nested(self) -> None:
        self.assertEqual(settings.get_setting(self.config, "agent.name"), "corax")
        self.assertEqual(settings.get_setting(self.config, "runtime.log_level"), "INFO")
        self.assertFalse(settings.get_setting(self.config, "security.allow_shell"))

    def test_set_string(self) -> None:
        settings.set_setting(self.config, "ui.theme", "dark")
        self.assertEqual(self.config.ui.theme, "dark")

    def test_set_bool_coerces(self) -> None:
        settings.set_setting(self.config, "security.allow_shell", "true")
        self.assertIs(self.config.security.allow_shell, True)
        settings.set_setting(self.config, "security.allow_shell", "no")
        self.assertIs(self.config.security.allow_shell, False)

    def test_set_int_coerces(self) -> None:
        settings.set_setting(self.config, "limits.max_parallel_tasks", "8")
        self.assertEqual(self.config.limits.max_parallel_tasks, 8)
        self.assertIsInstance(self.config.limits.max_parallel_tasks, int)

    def test_unknown_path_raises(self) -> None:
        with self.assertRaises(SettingError):
            settings.get_setting(self.config, "agent.nope")
        with self.assertRaises(SettingError):
            settings.set_setting(self.config, "nope.here", 1)


class TestProviders(unittest.TestCase):
    def setUp(self) -> None:
        self.config = cfg.default_config()

    def test_disable_then_enable(self) -> None:
        settings.toggle_provider(self.config, "planner", "stub", False)
        self.assertFalse(self.config.planner.providers["stub"].enabled)
        settings.toggle_provider(self.config, "planner", "stub", True)
        self.assertTrue(self.config.planner.providers["stub"].enabled)

    def test_disable_scalar_active_clears_active(self) -> None:
        settings.toggle_provider(self.config, "memory", "none", False)
        self.assertEqual(self.config.memory.active, "none")  # memory resets to 'none'

    def test_disable_list_active_removes_from_list(self) -> None:
        settings.toggle_provider(self.config, "connectors", "terminal", False)
        self.assertNotIn("terminal", self.config.connectors.active)

    def test_set_active_scalar(self) -> None:
        settings.set_active_provider(self.config, "planner", "stub")
        self.assertEqual(self.config.planner.active, "stub")

    def test_set_active_disabled_raises(self) -> None:
        settings.toggle_provider(self.config, "planner", "stub", False)
        with self.assertRaises(SettingError):
            settings.set_active_provider(self.config, "planner", "stub")

    def test_set_active_list_appends(self) -> None:
        settings.deactivate_provider(self.config, "connectors", "terminal")
        self.assertNotIn("terminal", self.config.connectors.active)
        settings.set_active_provider(self.config, "connectors", "terminal")
        self.assertIn("terminal", self.config.connectors.active)

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaises(SettingError):
            settings.toggle_provider(self.config, "planner", "ghost", True)
        with self.assertRaises(SettingError):
            settings.set_active_provider(self.config, "memory", "ghost")


if __name__ == "__main__":
    unittest.main()
