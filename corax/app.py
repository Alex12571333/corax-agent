"""Application layer.

:class:`CoraxApp` ties together config, paths, logging, runtime and the
menu into a clean boot/shutdown lifecycle:

    boot:     load config -> ensure paths -> setup logging -> init runtime -> start
    run_menu: show the settings menu
    shutdown: persist config if changed -> stop runtime
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import config as config_mod
from .config import AgentConfig
from .logging import setup_logging
from .paths import AgentPaths, ensure_paths
from .runtime import CoraxRuntime
from .ui.menu import Menu
from .ui.terminal import Terminal


class CoraxApp:
    """Boot/shutdown owner for a single agent process."""

    def __init__(self, config_path: Path, terminal: Terminal | None = None) -> None:
        self.config_path = Path(config_path)
        self.terminal = terminal or Terminal()

        self.config: AgentConfig | None = None
        self.paths: AgentPaths | None = None
        self.runtime: CoraxRuntime | None = None
        self.log: logging.Logger | None = None

        self._first_run = False
        self._dirty = False

    # -- lifecycle ------------------------------------------------------- #
    async def boot(self) -> None:
        self._load_or_create_config()
        self.paths = ensure_paths(self.config, self.config_path)
        self.log = setup_logging(self.config.runtime.log_level, self.paths.logs)
        self.log.debug("config loaded from %s", self.config_path)

        self.runtime = CoraxRuntime(
            self.config,
            logging.getLogger("corax.runtime"),
            root_path=self.paths.root,
            workspace_path=self.paths.workspace,
        )
        await self.runtime.start()

        if self._first_run:
            self._show_first_run_message()
            # Acknowledge first run and persist the flip on next save.
            self.config.agent.first_run = False
            self._dirty = True

    async def run_menu(self) -> str:
        if self.runtime is None or self.config is None:
            raise RuntimeError("boot() must be called before run_menu()")
        menu = Menu(
            config=self.config,
            config_path=self.config_path,
            runtime=self.runtime,
            terminal=self.terminal,
            save_fn=self._save_config,
        )
        result = menu.run()
        if menu.changed:
            self._dirty = True
        return result

    async def shutdown(self) -> None:
        if self._dirty and self.config is not None:
            self._save_config(self.config)
        if self.runtime is not None:
            await self.runtime.stop()
        if self.log is not None:
            self.log.debug("shutdown complete")

    # -- helpers --------------------------------------------------------- #
    def _load_or_create_config(self) -> None:
        if self.config_path.exists():
            self.config = config_mod.load_config(self.config_path)
        else:
            self.config = config_mod.create_default_config(self.config_path)
            self._first_run = True

    def _save_config(self, config: AgentConfig) -> None:
        config_mod.save_config(config, self.config_path)
        self._dirty = False
        if self.log is not None:
            self.log.info("config saved to %s", self.config_path)

    def _show_first_run_message(self) -> None:
        self.terminal.write("")
        self.terminal.write("First run: created default config and workspace/data/logs.")
        self.terminal.write(f"  config    : {self.config_path}")
        if self.paths is not None:
            self.terminal.write(f"  workspace : {self.paths.workspace}")
            self.terminal.write(f"  data      : {self.paths.data}")
            self.terminal.write(f"  logs      : {self.paths.logs}")
        self.terminal.write("")
