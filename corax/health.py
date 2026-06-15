"""Health payload shared by the built-in placeholder components.

Every built-in (planner / connector / memory / capability) reports its
status through this uniform, serialisable structure. Real implementations
are expected to return the same shape so status screens and (future)
health checks stay component-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Health:
    """Uniform health/status payload."""

    id: str
    kind: str
    healthy: bool = True
    detail: str = "ok"

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "healthy": self.healthy,
            "detail": self.detail,
        }
