"""Built-in deterministic planner provider."""

from __future__ import annotations

from agent_core import (
    CapabilityRequest,
    HealthStatus,
    PlannerProvider,
    Result,
)
from agent_sdk import model_provider

PLANNER_ID = "stub"


@model_provider(
    id=PLANNER_ID,
    name="Stub Planner",
    description="Produce one deterministic echo task without model inference.",
    version="0.2.0",
    interfaces=("agent.model/v1", "agent.planner/v1"),
    entrypoint="corax.planner.stub:StubPlanner",
)
class StubPlanner(PlannerProvider):
    async def plan(self, request: CapabilityRequest | str) -> Result | dict:
        legacy = isinstance(request, str)
        goal = request if legacy else str(request.input.get("goal", ""))
        tasks = [
            {
                "id": "task-1",
                "capability": "echo",
                "input": {"text": goal},
            }
        ]
        if legacy:
            return {"goal": goal, "correlation_id": None, "tasks": tasks}
        return Result.ok(
            {"tasks": tasks},
            session_id=request.session_id,
            task_id=request.task_id,
        )

    async def health_check(self) -> HealthStatus:
        return HealthStatus.HEALTHY
