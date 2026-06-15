"""Planner role.

The planner turns a goal into a list of capability tasks. Today only the
built-in :class:`~corax.planner.stub.StubPlanner` is shipped; LLM-backed
planners register under the same role later.
"""

from __future__ import annotations

from .stub import StubPlanner

__all__ = ["StubPlanner"]
