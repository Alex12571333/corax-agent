"""Built-in echo tool."""

from __future__ import annotations

from agent_core import (
    CapabilityRequest,
    HealthStatus,
    Result,
    ToolCapability,
)
from agent_sdk import tool

CAPABILITY_ID = "echo"


@tool(
    id=CAPABILITY_ID,
    name="Echo",
    description="Return the input unchanged.",
    version="0.2.0",
    tags=("text", "utility"),
    input_schema={"type": "object"},
    output_schema={"type": "object"},
    entrypoint="corax.capabilities.echo:EchoCapability",
)
class EchoCapability(ToolCapability):
    async def execute(self, request: CapabilityRequest) -> Result:
        return Result.ok(
            request.input,
            session_id=request.session_id,
            task_id=request.task_id,
        )

    async def health_check(self) -> HealthStatus:
        return HealthStatus.HEALTHY

    # Deprecated scaffold helpers.
    async def invoke(self, payload):
        return payload
