"""Registries — the extension seams of Corax.

A :class:`Registry` is a named, ordered collection of items keyed by id,
each carrying an ``enabled`` flag. Real connectors, memory backends,
providers and capabilities will register here at runtime; the scaffold
registers stubs. The interface is intentionally tiny so nothing about
the call sites changes when real implementations arrive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator


class RegistryError(KeyError):
    """Raised for unknown ids or duplicate registration."""


@dataclass
class RegistryEntry:
    id: str
    item: Any
    enabled: bool = True


class Registry:
    """A minimal id -> item store with enable/disable semantics."""

    #: Override in subclasses for nicer logs / introspection.
    kind: str = "item"

    def __init__(self, name: str | None = None) -> None:
        self.name = name or self.__class__.__name__
        self._entries: dict[str, RegistryEntry] = {}

    # -- mutation -------------------------------------------------------- #
    def register(self, id: str, item: Any, enabled: bool = True) -> None:
        if id in self._entries:
            raise RegistryError(f"{self.name}: '{id}' is already registered")
        self._entries[id] = RegistryEntry(id=id, item=item, enabled=enabled)

    def unregister(self, id: str) -> None:
        if id not in self._entries:
            raise RegistryError(f"{self.name}: '{id}' is not registered")
        del self._entries[id]

    def enable(self, id: str) -> None:
        self._entry(id).enabled = True

    def disable(self, id: str) -> None:
        self._entry(id).enabled = False

    def clear(self) -> None:
        self._entries.clear()

    # -- access ---------------------------------------------------------- #
    def get(self, id: str) -> Any:
        return self._entry(id).item

    def is_enabled(self, id: str) -> bool:
        return self._entry(id).enabled

    def has(self, id: str) -> bool:
        return id in self._entries

    def ids(self) -> list[str]:
        return list(self._entries.keys())

    def list_all(self) -> list[RegistryEntry]:
        return list(self._entries.values())

    def list_enabled(self) -> list[RegistryEntry]:
        return [e for e in self._entries.values() if e.enabled]

    # -- dunder ---------------------------------------------------------- #
    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[RegistryEntry]:
        return iter(self._entries.values())

    def __contains__(self, id: object) -> bool:
        return id in self._entries

    def _entry(self, id: str) -> RegistryEntry:
        try:
            return self._entries[id]
        except KeyError:
            raise RegistryError(f"{self.name}: '{id}' is not registered") from None


from .capabilities import CapabilityRegistryAdapter  # noqa: E402
from .connectors import ConnectorRegistry  # noqa: E402
from .memory import MemoryRegistry  # noqa: E402
from .providers import ProviderRegistry  # noqa: E402

__all__ = [
    "Registry",
    "RegistryEntry",
    "RegistryError",
    "ConnectorRegistry",
    "MemoryRegistry",
    "ProviderRegistry",
    "CapabilityRegistryAdapter",
]
