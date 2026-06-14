"""Memory registry.

Holds memory backends. The scaffold registers only the no-op
:class:`~corax_agent.stubs.memory_stub.MemoryStub`. SQLite / vector
stores plug in here later.
"""

from __future__ import annotations

from . import Registry


class MemoryRegistry(Registry):
    kind = "memory"

    def __init__(self) -> None:
        super().__init__("MemoryRegistry")
