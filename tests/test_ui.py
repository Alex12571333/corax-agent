from __future__ import annotations

import unittest

from corax.ui.terminal import Terminal


class TerminalTest(unittest.TestCase):
    def test_injected_writer_uses_plain_theme(self) -> None:
        output: list[str] = []
        terminal = Terminal(writer=output.append)

        terminal.header("Runtime")
        terminal.banner()

        self.assertFalse(terminal.theme.enabled)
        self.assertNotIn("\x1b", "\n".join(output))

    def test_untrusted_terminal_controls_are_removed(self) -> None:
        output: list[str] = []
        terminal = Terminal(writer=output.append)

        terminal.write("safe\x1b[31mred\x1b[0m\x07")

        self.assertEqual(output, ["safered"])


if __name__ == "__main__":
    unittest.main()
