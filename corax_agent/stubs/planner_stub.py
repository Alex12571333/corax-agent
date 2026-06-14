"""Stub planner.

Does not call an LLM. Given a goal, it returns a single, trivial echo
task so the rest of the pipeline has something well-formed to carry. A
real planner (e.g. an LLM-backed one) will replace it under the same
``provider`` role.
"""

from __future__ import annotations

from typing import Any

from . import StubHealth

PLANNER_ID = "planner.stub"


class PlannerStub:
    id = PLANNER_ID
    kind = "planner"

    def describe(self) -> str:
        return "Local stub planner — produces a single echo task, no LLM."

    async def plan(self, goal: str, *, correlation_id: str | None = None) -> dict[str, Any]:
        """Return a minimal, deterministic plan for ``goal``."""
        return {
            "goal": goal,
            "correlation_id": correlation_id,
            "tasks": [
                {
                    "id": "task-1",
                    "capability": "stub.echo",
                    "input": {"text": goal},
                }
            ],
        }

    async def health(self) -> StubHealth:
        return StubHealth(id=self.id, kind=self.kind, detail="planner stub ready")
