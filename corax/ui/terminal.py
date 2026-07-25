"""Plain-terminal I/O.

A thin wrapper around ``input``/``print`` with injectable callables so
the menu is fully testable: tests pass a fake reader/writer and assert on
captured output without real stdin. No curses, no third-party TUI.
"""

from __future__ import annotations

import sys
from typing import Callable

from corax_ui import TerminalTheme, safe_text


class Terminal:
    """Injectable terminal I/O surface."""

    def __init__(
        self,
        reader: Callable[[str], str] | None = None,
        writer: Callable[[str], None] | None = None,
        theme: TerminalTheme | None = None,
    ) -> None:
        self._reader = reader or input
        self._writer = writer or print
        self.theme = theme or (
            TerminalTheme.plain()
            if writer is not None
            else TerminalTheme.detect(sys.stdout)
        )

    # -- output ---------------------------------------------------------- #
    def write(self, text: str = "") -> None:
        self._writer(safe_text(text))

    def lines(self, items: list[str]) -> None:
        for item in items:
            self._writer(safe_text(item))

    def banner(self) -> None:
        self._writer(self.theme.logo())

    def header(self, title: str) -> None:
        self._writer(self.theme.header(title))

    def divider(self) -> None:
        self._writer(self.theme.rule())

    # -- input ----------------------------------------------------------- #
    def read(self, prompt: str = "") -> str:
        """Read one line. EOF (Ctrl-D / exhausted fake input) raises EOFError."""
        try:
            return self._reader(self.theme.paint(prompt, "accent")).strip()
        except (EOFError, StopIteration):
            raise EOFError from None

    def read_default(self, prompt: str, default: str) -> str:
        """Read a line, returning ``default`` when the user enters nothing."""
        value = self.read(f"{prompt} [{default}]: ")
        return value if value else default
