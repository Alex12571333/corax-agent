"""Synchronous composition-root registries keyed by extension role."""

from __future__ import annotations

from typing import Any, Iterator

from agent_core import ExtensionKind

from . import Registry, RegistryEntry, RegistryError


class TypedExtensionRegistry(Registry):
    def __init__(self, kind: ExtensionKind) -> None:
        self.extension_kind = kind
        self.kind = kind.value
        super().__init__(f"{kind.value}Registry")

    def register(self, id: str, item: Any) -> None:
        actual = getattr(item, "kind", None)
        if isinstance(actual, str):
            try:
                actual = ExtensionKind(actual)
            except ValueError:
                pass
        if actual is not self.extension_kind:
            raise RegistryError(
                f"{self.name}: '{id}' declares kind "
                f"{getattr(actual, 'value', actual)!r}, expected "
                f"{self.extension_kind.value!r}"
            )
        declared_id = getattr(item, "id", None)
        if declared_id != id:
            raise RegistryError(
                f"{self.name}: config id {id!r} does not match "
                f"extension id {declared_id!r}"
            )
        super().register(id, item)


class ExtensionCatalog:
    """All runtime extensions, partitioned by their declared kind."""

    def __init__(self) -> None:
        self._registries = {
            kind: TypedExtensionRegistry(kind) for kind in ExtensionKind
        }

    def registry(self, kind: ExtensionKind | str) -> TypedExtensionRegistry:
        resolved = kind if isinstance(kind, ExtensionKind) else ExtensionKind(kind)
        return self._registries[resolved]

    def register(self, id: str, item: Any) -> None:
        self.registry(getattr(item, "kind")).register(id, item)

    def get(self, id: str) -> Any:
        for registry in self._registries.values():
            if registry.has(id):
                return registry.get(id)
        raise RegistryError(f"ExtensionCatalog: '{id}' is not registered")

    def has(self, id: str) -> bool:
        return any(registry.has(id) for registry in self._registries.values())

    def clear(self) -> None:
        for registry in self._registries.values():
            registry.clear()

    def active_by_kind(self) -> dict[str, list[str]]:
        return {
            kind.value: registry.ids()
            for kind, registry in self._registries.items()
            if len(registry)
        }

    def __iter__(self) -> Iterator[RegistryEntry]:
        for registry in self._registries.values():
            yield from registry

    def __len__(self) -> int:
        return sum(len(registry) for registry in self._registries.values())
