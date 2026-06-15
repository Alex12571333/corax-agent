"""The ``none`` memory backend.

Stores nothing and always reports empty. Useful so the memory registry is
never empty and call sites can assume a backend exists. A SQLite / vector
store replaces it under the same ``memory`` role.
"""

from __future__ import annotations

from typing import Any

from ..health import Health

MEMORY_ID = "memory.none"


class NullMemory:
    id = MEMORY_ID
    kind = "memory"

    def describe(self) -> str:
        return "No-op memory — nothing is persisted, queries return empty."

    async def store(self, *args: Any, **kwargs: Any) -> bool:
        """Pretend to store; always reports failure (no memory configured)."""
        return False

    async def query(self, *args: Any, **kwargs: Any) -> list[Any]:
        """Always return an empty result set."""
        return []

    async def health(self) -> Health:
        return Health(id=self.id, kind=self.kind, detail="no persistent memory")
