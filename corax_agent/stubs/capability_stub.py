"""Stub capability.

The ``stub.echo`` tool: returns its input unchanged. It is the canonical
example of a capability and the target of the stub planner's single
task. Real capabilities (filesystem, shell, HTTP, MCP) register under
the capability registry the same way.
"""

from __future__ import annotations

from typing import Any

from . import StubHealth

CAPABILITY_ID = "stub.echo"


class CapabilityStub:
    id = CAPABILITY_ID
    kind = "capability"

    def describe(self) -> str:
        return "Echo capability — returns the given input unchanged."

    async def invoke(self, payload: Any) -> Any:
        """Echo ``payload`` straight back."""
        return payload

    async def health(self) -> StubHealth:
        return StubHealth(id=self.id, kind=self.kind, detail="echo ready")
