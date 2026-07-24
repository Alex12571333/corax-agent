"""Connector registry.

Connectors are the agent's I/O surfaces (terminal today; Telegram, HTTP,
etc. later). This registry holds them; the scaffold only ever registers
the built-in :class:`~corax.connectors.terminal.TerminalConnector`.
"""

from __future__ import annotations

from agent_core import ExtensionKind

from .extensions import TypedExtensionRegistry


class ConnectorRegistry(TypedExtensionRegistry):
    def __init__(self) -> None:
        super().__init__(ExtensionKind.CHANNEL_CONNECTOR)
