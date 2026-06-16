"""Gateway policy engine.

The core's ``DefaultPolicyEngine`` parks every CONFIRM capability behind a human
prompt and denies DANGEROUS outright — sensible for an interactive operator, but
it would stall an unattended chat loop that needs every tool available.

This engine is intentionally **permissive**: it approves everything except
BLOCKED, so the model can reach all of the agent's capabilities (including
DANGEROUS ones like ``shell``). Real authorization is meant to live in a
dedicated policy plugin layered on top later.

⚠️  SECURITY: with this policy a Telegram chat can drive any capability,
including running shell commands on the host. Restrict who can talk to the bot
with ``CORAX_TELEGRAM_ALLOWED_CHATS`` and replace this with a real policy before
exposing it beyond yourself.

Plugs into the kernel through the public ``agent_core.PolicyEngine`` ABC — no
change to ``agent-core`` is required.
"""

from __future__ import annotations

from agent_core import (
    DecisionType,
    PermissionLevel,
    PolicyDecision,
    PolicyEngine,
)

_DENY = {PermissionLevel.BLOCKED}


class GatewayPolicyEngine(PolicyEngine):
    """Approve every capability except BLOCKED (authorization deferred to a plugin)."""

    async def evaluate(self, task, capability, context) -> PolicyDecision:
        if context.permission_level in _DENY:
            return PolicyDecision(
                decision=DecisionType.DENY,
                reason=f"gateway denies permission level {context.permission_level.value}",
                metadata={"capability_id": context.capability_id},
            )
        return PolicyDecision(
            decision=DecisionType.ALLOW,
            reason="gateway approves (authorization deferred to a dedicated policy plugin)",
            metadata={"capability_id": context.capability_id},
        )
