"""Stub connector.

Represents the ``terminal`` connector slot. It performs no real chat or
I/O yet — only health/status — so the connector registry has a concrete
member. Telegram / HTTP connectors slot in under the same role later.
"""

from __future__ import annotations

from . import StubHealth

CONNECTOR_ID = "connector.terminal"


class ConnectorStub:
    id = CONNECTOR_ID
    kind = "connector"

    def __init__(self, provider_id: str = "terminal") -> None:
        self.provider_id = provider_id

    def describe(self) -> str:
        return f"Terminal connector placeholder ('{self.provider_id}') — no real I/O yet."

    async def health(self) -> StubHealth:
        return StubHealth(
            id=self.id,
            kind=self.kind,
            detail=f"connector '{self.provider_id}' placeholder",
        )

    async def status(self) -> dict[str, object]:
        return {"id": self.id, "provider": self.provider_id, "connected": False}
