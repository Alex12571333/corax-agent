"""The ``echo`` capability.

Returns its input unchanged. It is the canonical example of a capability
and the target of the stub planner's single task. Real capabilities
(filesystem, editor, shell, HTTP, MCP) register under the capability
registry the same way — most are loaded from standalone SDK packages by
:mod:`corax.loader.capabilities`.
"""

from __future__ import annotations

from typing import Any

from ..health import Health

CAPABILITY_ID = "echo"


class EchoCapability:
    id = CAPABILITY_ID
    kind = "capability"

    def describe(self) -> str:
        return "Echo capability — returns the given input unchanged."

    async def invoke(self, payload: Any) -> Any:
        """Echo ``payload`` straight back."""
        return payload

    async def health(self) -> Health:
        return Health(id=self.id, kind=self.kind, detail="echo ready")
