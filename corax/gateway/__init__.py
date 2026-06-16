"""Gateway: drive a chat platform through the agent-core execution kernel.

The gateway is an optional feature that requires ``agent-core``. It is imported
lazily (only when ``main.py --chat`` runs), so the rest of the scaffold still
works on a pure-stdlib install.
"""

from __future__ import annotations

from .telegram_gateway import CoraxTelegramGateway, GatewayError

__all__ = ["CoraxTelegramGateway", "GatewayError"]
