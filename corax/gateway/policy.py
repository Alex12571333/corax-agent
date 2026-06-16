"""Gateway policy engine.

The core's ``DefaultPolicyEngine`` parks every CONFIRM capability behind a human
prompt — sensible for an interactive operator, but it would stall an unattended
chat loop. The gateway runs a known, trusted set of connectors (``llm.local``
and ``telegram.connector`` are CONFIRM) and needs them to execute without a
prompt, while still refusing anything DANGEROUS or BLOCKED.

This plugs into the kernel through the public ``agent_core.PolicyEngine`` ABC —
no change to ``agent-core`` is required. ``agent-core`` is imported at module
load, so this module is only imported on the gateway path.
"""

from __future__ import annotations

from agent_core import (
    DecisionType,
    PermissionLevel,
    PolicyDecision,
    PolicyEngine,
)

_AUTO_APPROVE = {PermissionLevel.SAFE, PermissionLevel.CONFIRM}


class GatewayPolicyEngine(PolicyEngine):
    """Approve SAFE/CONFIRM capabilities for the gateway; deny the rest."""

    async def evaluate(self, task, capability, context) -> PolicyDecision:
        if context.permission_level in _AUTO_APPROVE:
            return PolicyDecision(
                decision=DecisionType.ALLOW,
                reason="gateway approves safe/confirm capability",
                metadata={"capability_id": context.capability_id},
            )
        return PolicyDecision(
            decision=DecisionType.DENY,
            reason=f"gateway denies permission level {context.permission_level.value}",
            metadata={"capability_id": context.capability_id},
        )
