"""Provider registry.

A "provider" is anything that backs a pluggable role — most importantly
the planner. The scaffold registers the
:class:`~corax_agent.stubs.planner_stub.PlannerStub`. Real LLM planners
register here later without touching the runtime.
"""

from __future__ import annotations

from . import Registry


class ProviderRegistry(Registry):
    kind = "provider"

    def __init__(self) -> None:
        super().__init__("ProviderRegistry")
