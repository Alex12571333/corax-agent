"""Shared test helpers: scripted terminal I/O."""

from __future__ import annotations

from corax_agent.ui.terminal import Terminal


class ScriptedReader:
    """A reader that yields queued inputs, then raises StopIteration (-> EOF)."""

    def __init__(self, inputs: list[str]) -> None:
        self._it = iter(inputs)

    def __call__(self, prompt: str = "") -> str:
        return next(self._it)


class Capture:
    """A writer that records every line written."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def scripted_terminal(inputs: list[str]) -> tuple[Terminal, Capture]:
    capture = Capture()
    term = Terminal(reader=ScriptedReader(inputs), writer=capture)
    return term, capture
