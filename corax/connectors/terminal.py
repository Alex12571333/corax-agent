"""Built-in terminal channel connector."""

from __future__ import annotations

from agent_core import (
    ChannelConnector,
    ChannelMessage,
    HealthStatus,
    Result,
)
from agent_sdk import channel_connector

CONNECTOR_ID = "terminal"


@channel_connector(
    id=CONNECTOR_ID,
    name="Terminal",
    description="Built-in terminal channel.",
    version="0.2.0",
    entrypoint="corax.connectors.terminal:TerminalConnector",
)
class TerminalConnector(ChannelConnector):
    def __init__(self, provider_id: str = "terminal") -> None:
        self.provider_id = provider_id

    async def receive(self, *, limit: int = 1):
        return []

    async def send(self, message: ChannelMessage) -> Result:
        return Result.ok(
            {
                "sent": False,
                "channel": "terminal",
                "conversation_id": message.conversation_id,
                "text": message.text,
            },
            session_id="",
        )

    async def health_check(self) -> HealthStatus:
        return HealthStatus.DEGRADED

    async def status(self) -> dict[str, object]:
        return {"id": self.id, "provider": self.provider_id, "connected": False}
