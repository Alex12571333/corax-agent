"""Stub implementations used by the scaffold.

Every stub here is a *placeholder* with a stable id and a small async
surface. They let the runtime, registries and menu work end-to-end with
zero external dependencies. Replacing a stub with a real implementation
means registering a different object under the same role — nothing in
the runtime needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StubHealth:
    """Uniform health/status payload returned by all stubs."""

    id: str
    kind: str
    healthy: bool = True
    detail: str = "stub"

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "healthy": self.healthy,
            "detail": self.detail,
        }


from .capability_stub import CapabilityStub  # noqa: E402
from .connector_stub import ConnectorStub  # noqa: E402
from .memory_stub import MemoryStub  # noqa: E402
from .planner_stub import PlannerStub  # noqa: E402

__all__ = [
    "StubHealth",
    "PlannerStub",
    "ConnectorStub",
    "MemoryStub",
    "CapabilityStub",
]
