"""Terminal settings menu.

A small, dependency-free menu driven by :class:`~corax.ui.terminal.Terminal`.
All config edits go through :mod:`corax.settings`, so the menu never
mutates dataclasses directly. I/O is injectable, which makes every screen
unit-testable with a scripted list of inputs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .. import settings
from ..config import AgentConfig
from ..paths import AgentPaths
from ..runtime import CoraxRuntime
from ..settings import SettingError
from . import screens
from .terminal import Terminal


class Menu:
    """Interactive settings menu over an :class:`AgentConfig`."""

    def __init__(
        self,
        config: AgentConfig,
        config_path: Path,
        runtime: CoraxRuntime | None = None,
        terminal: Terminal | None = None,
        save_fn: Callable[[AgentConfig], None] | None = None,
    ) -> None:
        self.config = config
        self.config_path = Path(config_path)
        self.runtime = runtime
        self.term = terminal or Terminal()
        self._save_fn = save_fn
        self.changed = False
        self._dispatch: dict[str, Callable[[], None]] = {
            "1": self.show_status,
            "2": self.runtime_settings,
            "3": self.planner_section,
            "4": self.memory_section,
            "5": self.connectors_section,
            "6": self.capabilities_section,
            "7": self.security_section,
            "8": self.limits_section,
            "9": self.paths_section,
            "l": self.llm_section,
            "L": self.llm_section,
        }

    # -- main loop ------------------------------------------------------- #
    def run(self) -> str:
        """Run the menu loop. Returns 'saved', 'discarded' or 'eof'."""
        if self.config.ui.show_banner:
            self.term.banner()
        while True:
            self.term.lines(screens.MAIN_MENU)
            try:
                choice = self.term.read("select> ")
                if choice == "10":
                    self._save()
                    return "saved"
                if choice == "0":
                    return "discarded"
                handler = self._dispatch.get(choice)
                if handler is not None:
                    handler()
                elif choice:
                    self.term.write(f"  unknown option: {choice!r}")
            except EOFError:
                return "eof"

    # -- screens --------------------------------------------------------- #
    def show_status(self) -> None:
        self.term.header("Status")
        if self.runtime is None:
            self.term.write("  (runtime not initialised)")
            return
        self.term.lines(screens.status_screen(self.runtime.snapshot()))

    def runtime_settings(self) -> None:
        while True:
            self.term.header("Runtime Settings")
            self.term.lines(screens.runtime_screen(self.config))
            choice = self.term.read("edit # (enter to go back)> ")
            if not choice:
                return
            if choice == "1":
                self._edit_scalar("runtime.log_level", "log level")
            elif choice == "2":
                self._toggle("runtime.autostart")
            elif choice == "3":
                self._edit_scalar("runtime.workspace_path", "workspace path")
            elif choice == "4":
                self._edit_scalar("runtime.data_path", "data path")
            elif choice == "5":
                self._edit_scalar("runtime.logs_path", "logs path")
            else:
                self.term.write(f"  unknown field: {choice!r}")

    def planner_section(self) -> None:
        self._provider_section(
            section="planner",
            title="Planner",
            providers=self.config.planner.providers,
            active=[self.config.planner.active],
            list_based=False,
        )

    def memory_section(self) -> None:
        self._provider_section(
            section="memory",
            title="Memory",
            providers=self.config.memory.providers,
            active=[self.config.memory.active],
            list_based=False,
        )

    def connectors_section(self) -> None:
        self._provider_section(
            section="connectors",
            title="Connectors",
            providers=self.config.connectors.providers,
            active=self.config.connectors.active,
            list_based=True,
        )

    def capabilities_section(self) -> None:
        self._provider_section(
            section="capabilities",
            title="Capabilities",
            providers=self.config.capabilities.available,
            active=self.config.capabilities.enabled,
            list_based=True,
        )

    def security_section(self) -> None:
        while True:
            self.term.header("Security")
            self.term.lines(screens.security_screen(self.config))
            choice = self.term.read("edit # (enter to go back)> ")
            if not choice:
                return
            if choice == "1":
                self._edit_scalar("security.mode", "security mode")
            elif choice == "2":
                self._toggle("security.core_readonly")
            elif choice == "3":
                self._toggle("security.allow_shell")
            elif choice == "4":
                self._toggle("security.allow_file_write")
            else:
                self.term.write(f"  unknown field: {choice!r}")

    def limits_section(self) -> None:
        fields = {
            "1": "limits.max_parallel_tasks",
            "2": "limits.max_plan_tasks",
            "3": "limits.max_tasks_per_correlation",
            "4": "limits.task_timeout_seconds",
            "5": "limits.max_payload_mb",
        }
        while True:
            self.term.header("Limits")
            self.term.lines(screens.limits_screen(self.config))
            choice = self.term.read("edit # (enter to go back)> ")
            if not choice:
                return
            key = fields.get(choice)
            if key is None:
                self.term.write(f"  unknown field: {choice!r}")
                continue
            self._edit_scalar(key, key.split(".")[-1])

    def llm_section(self) -> None:
        while True:
            self.term.header("LLM Local Connector")
            self.term.lines(screens.llm_screen(self.config))
            choice = self.term.read("edit # (enter to go back)> ")
            if not choice:
                return
            if choice == "1":
                self._edit_scalar("llm.base_url", "endpoint base url")
            elif choice == "2":
                self._edit_scalar("llm.model", "model id")
            elif choice == "3":
                self._toggle("llm.enable_image")
            elif choice == "4":
                self._toggle("llm.enable_video")
            else:
                self.term.write(f"  unknown field: {choice!r}")

    def paths_section(self) -> None:
        self.term.header("Paths")
        paths = AgentPaths.from_config(self.config, self.config_path)
        self.term.lines(screens.paths_screen(paths))

    # -- shared provider workflow --------------------------------------- #
    def _provider_section(
        self, section, title, providers, active, list_based: bool
    ) -> None:
        while True:
            self.term.header(title)
            self.term.lines(screens.providers_screen(title, providers, active))
            self.term.write("")
            self.term.write("  actions: [e]nable  [d]isable  [a]ctivate" +
                            ("  [x] deactivate" if list_based else ""))
            action = self.term.read("action (enter to go back)> ")
            if not action:
                return
            action = action.lower()
            if action not in ("e", "d", "a", "x"):
                self.term.write(f"  unknown action: {action!r}")
                continue
            provider_id = self.term.read("provider id> ")
            if not provider_id:
                continue
            try:
                if action == "e":
                    settings.toggle_provider(self.config, section, provider_id, True)
                elif action == "d":
                    settings.toggle_provider(self.config, section, provider_id, False)
                elif action == "a":
                    settings.set_active_provider(self.config, section, provider_id)
                elif action == "x":
                    settings.deactivate_provider(self.config, section, provider_id)
            except SettingError as exc:
                self.term.write(f"  error: {exc}")
                continue
            self.changed = True
            self.term.write(f"  ok: {action} {provider_id}")
            # refresh local references to the (possibly new) active lists
            active = self._refresh_active(section)

    def _refresh_active(self, section: str) -> list[str]:
        if section == "planner":
            return [self.config.planner.active]
        if section == "memory":
            return [self.config.memory.active]
        if section == "connectors":
            return self.config.connectors.active
        if section == "capabilities":
            return self.config.capabilities.enabled
        return []

    # -- editing helpers ------------------------------------------------- #
    def _edit_scalar(self, key_path: str, label: str) -> None:
        current = settings.get_setting(self.config, key_path)
        value = self.term.read(f"new {label} [{current}]> ")
        if not value:
            return
        try:
            settings.set_setting(self.config, key_path, value)
        except (ValueError, SettingError) as exc:
            self.term.write(f"  invalid value: {exc}")
            return
        self.changed = True
        self.term.write(f"  set {key_path} = {settings.get_setting(self.config, key_path)}")

    def _toggle(self, key_path: str) -> None:
        current = bool(settings.get_setting(self.config, key_path))
        settings.set_setting(self.config, key_path, not current)
        self.changed = True
        self.term.write(f"  set {key_path} = {not current}")

    def _save(self) -> None:
        self.config.agent.first_run = False
        if self._save_fn is not None:
            self._save_fn(self.config)
        self.changed = False
        self.term.write(f"  saved -> {self.config_path}")
