"""Memory registry.

Holds memory backends. The scaffold registers only the no-op
:class:`~corax.memory.none.NullMemory`. SQLite / vector stores plug in
here later.
"""

from __future__ import annotations

from agent_core import ExtensionKind

from .extensions import TypedExtensionRegistry


class MemoryRegistry(TypedExtensionRegistry):
    def __init__(self) -> None:
        super().__init__(ExtensionKind.MEMORY_PROVIDER)
