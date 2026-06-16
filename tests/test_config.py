"""Config: defaults, validation, round-trip I/O, blocked-path guard."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from corax import config as cfg
from corax.paths import is_blocked_path


class TestDefaultConfig(unittest.TestCase):
    def test_create_default_writes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.json"
            config = cfg.create_default_config(path)
            self.assertTrue(path.exists())
            self.assertEqual(config.agent.name, "corax")
            self.assertTrue(config.agent.first_run)

    def test_default_has_all_required_sections(self) -> None:
        config = cfg.default_config()
        for section in cfg.REQUIRED_SECTIONS:
            self.assertTrue(hasattr(config, section), f"missing {section}")

    def test_default_active_references_exist(self) -> None:
        config = cfg.default_config()
        self.assertIn(config.planner.active, config.planner.providers)
        self.assertIn(config.memory.active, config.memory.providers)
        for cid in config.connectors.active:
            self.assertIn(cid, config.connectors.providers)
        for cap in config.capabilities.enabled:
            self.assertIn(cap, config.capabilities.available)


class TestValidation(unittest.TestCase):
    def test_default_config_is_valid(self) -> None:
        self.assertEqual(cfg.validate_config(cfg.default_config()), [])

    def test_bad_log_level_flagged(self) -> None:
        config = cfg.default_config()
        config.runtime.log_level = "LOUD"
        errors = cfg.validate_config(config)
        self.assertTrue(any("log_level" in e for e in errors))

    def test_missing_active_planner_flagged(self) -> None:
        config = cfg.default_config()
        config.planner.active = "ghost"
        errors = cfg.validate_config(config)
        self.assertTrue(any("planner.active" in e for e in errors))

    def test_non_positive_limit_flagged(self) -> None:
        config = cfg.default_config()
        config.limits.max_parallel_tasks = 0
        errors = cfg.validate_config(config)
        self.assertTrue(any("max_parallel_tasks" in e for e in errors))


class TestRoundTrip(unittest.TestCase):
    def _round_trip(self, filename: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / filename
            original = cfg.default_config()
            original.runtime.log_level = "DEBUG"
            original.security.allow_shell = True
            cfg.save_config(original, path)
            loaded = cfg.load_config(path)
            self.assertEqual(loaded.runtime.log_level, "DEBUG")
            self.assertTrue(loaded.security.allow_shell)
            self.assertEqual(loaded.to_dict(), original.to_dict())

    def test_json_round_trip(self) -> None:
        self._round_trip("agent.json")

    def test_yaml_round_trip(self) -> None:
        # Works with PyYAML or the built-in fallback parser.
        self._round_trip("agent.yaml")

    def test_blocked_paths_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.yaml"
            cfg.save_config(cfg.default_config(), path)
            loaded = cfg.load_config(path)
            self.assertIn("../corax-core", loaded.security.blocked_paths)
            self.assertIn("../corax-sdk", loaded.security.blocked_paths)


class TestLLMConfig(unittest.TestCase):
    def test_default_llm_section_and_registration(self) -> None:
        config = cfg.default_config()
        self.assertEqual(config.llm.base_url, "http://192.168.0.10:8000/v1")
        self.assertEqual(config.llm.model, "google/gemma-4-12B-it")
        self.assertFalse(config.llm.enable_image)
        self.assertFalse(config.llm.enable_video)
        # The connector is registered as an installable, enabled capability.
        self.assertIn("llm.local", config.capabilities.enabled)
        self.assertIn("llm.local", config.capabilities.available)
        self.assertEqual(
            config.capabilities.available["llm.local"].path,
            "../corax-llm-local-connector",
        )

    def test_llm_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.yaml"
            original = cfg.default_config()
            original.llm.enable_image = True
            original.llm.model = "qwen3.6-35b-a3b"
            cfg.save_config(original, path)
            loaded = cfg.load_config(path)
            self.assertTrue(loaded.llm.enable_image)
            self.assertFalse(loaded.llm.enable_video)
            self.assertEqual(loaded.llm.model, "qwen3.6-35b-a3b")
            self.assertEqual(loaded.to_dict(), original.to_dict())


class TestBlockedPathGuard(unittest.TestCase):
    """The agent must never be allowed to write into corax-core / corax-sdk."""

    def test_core_and_sdk_are_blocked(self) -> None:
        config = cfg.default_config()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "corax-agent"
            base.mkdir()
            self.assertTrue(is_blocked_path(config, Path("../corax-core"), base))
            self.assertTrue(is_blocked_path(config, Path("../corax-sdk"), base))
            self.assertTrue(
                is_blocked_path(config, Path("../corax-core/src/x.py"), base)
            )

    def test_workspace_is_not_blocked(self) -> None:
        config = cfg.default_config()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "corax-agent"
            base.mkdir()
            self.assertFalse(is_blocked_path(config, Path("workspace/out.txt"), base))


if __name__ == "__main__":
    unittest.main()
