"""Plain-terminal I/O.

A thin wrapper around ``input``/``print`` with injectable callables so
the menu is fully testable: tests pass a fake reader/writer and assert on
captured output without real stdin. No curses, no third-party TUI.
"""

from __future__ import annotations

from typing import Callable

from .banner import BANNER


class Terminal:
    """Injectable terminal I/O surface."""

    def __init__(
        self,
        reader: Callable[[str], str] | None = None,
        writer: Callable[[str], None] | None = None,
    ) -> None:
        self._reader = reader or input
        self._writer = writer or print

    # -- output ---------------------------------------------------------- #
    def write(self, text: str = "") -> None:
        self._writer(text)

    def lines(self, items: list[str]) -> None:
        for item in items:
            self._writer(item)

    def banner(self) -> None:
        self._writer(BANNER.rstrip("\n"))

    def header(self, title: str) -> None:
        bar = "=" * max(len(title), 32)
        self._writer("")
        self._writer(bar)
        self._writer(title)
        self._writer(bar)

    def divider(self) -> None:
        self._writer("-" * 32)

    # -- input ----------------------------------------------------------- #
    def read(self, prompt: str = "") -> str:
        """Read one line. EOF (Ctrl-D / exhausted fake input) raises EOFError."""
        try:
            return self._reader(prompt).strip()
        except (EOFError, StopIteration):
            raise EOFError from None

    def read_default(self, prompt: str, default: str) -> str:
        """Read a line, returning ``default`` when the user enters nothing."""
        value = self.read(f"{prompt} [{default}]: ")
        return value if value else default
