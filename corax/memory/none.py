"""Built-in no-op memory provider."""

from __future__ import annotations

from agent_core import (
    HealthStatus,
    MemoryProvider,
    MemoryQuery,
    MemoryRecord,
    Result,
)
from agent_sdk import memory_provider

MEMORY_ID = "memory.none"


@memory_provider(
    id=MEMORY_ID,
    name="No Memory",
    description="No-op memory provider.",
    version="0.2.0",
    entrypoint="corax.memory.none:NullMemory",
)
class NullMemory(MemoryProvider):
    async def remember(self, record: MemoryRecord) -> Result:
        return Result.ok({"stored": False}, session_id="")

    async def recall(self, query: MemoryQuery) -> Result:
        return Result.ok({"results": [], "query": query.text}, session_id="")

    async def forget(self, memory_id: str, *, scope=None) -> Result:
        return Result.ok(
            {"forgotten": False, "memory_id": memory_id},
            session_id="",
        )

    async def health_check(self) -> HealthStatus:
        return HealthStatus.DEGRADED

    # 0.1 compatibility.
    async def store(self, *args, **kwargs) -> bool:
        return False

    async def query(self, *args, **kwargs) -> list:
        return []
