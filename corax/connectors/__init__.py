"""Connector role.

Connectors are the agent's I/O surfaces (terminal today; Telegram, HTTP,
etc. later). The built-in
:class:`~corax.connectors.terminal.TerminalConnector` is the only one
shipped today.
"""

from __future__ import annotations

from .terminal import TerminalConnector

__all__ = ["TerminalConnector"]
