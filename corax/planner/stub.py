"""Built-in stub planner.

Does not call an LLM. Given a goal, it returns a single, trivial echo
task so the rest of the pipeline has something well-formed to carry. A
real planner (e.g. an LLM-backed one) replaces it under the same
``planner`` role — see docs/EXTENDING.md.
"""

from __future__ import annotations

from typing import Any

from ..health import Health

PLANNER_ID = "planner.stub"


class StubPlanner:
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
                    "capability": "echo",
                    "input": {"text": goal},
                }
            ],
        }

    async def health(self) -> Health:
        return Health(id=self.id, kind=self.kind, detail="planner stub ready")
