"""Capability registry adapter.

Capabilities are the tools the agent can invoke (echo today; filesystem,
shell, HTTP, MCP tools later). It is named an *adapter* because in a
later stage it is expected to wrap / delegate to the capability registry
provided by ``corax-core`` / ``corax-sdk`` rather than owning the data
itself. For now it behaves like a plain :class:`Registry`.
"""

from __future__ import annotations

from . import Registry


class CapabilityRegistryAdapter(Registry):
    kind = "capability"

    def __init__(self) -> None:
        super().__init__("CapabilityRegistryAdapter")
