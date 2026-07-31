"""Typed extension package loader."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import ExtensionSpec

_WORKSPACE_CONFINED = {"filesystem", "editor"}


class ExtensionLoader:
    """Load one package after manifest, kind and contract validation."""

    def __init__(
        self,
        *,
        root_path: str | Path,
        workspace_path: str | Path,
        core_version: str = "0.2.1",
        log: logging.Logger | None = None,
    ) -> None:
        self.root_path = Path(root_path)
        self.workspace_path = Path(workspace_path)
        self.core_version = core_version
        self.log = log or logging.getLogger("corax.loader")
        self.manifests: dict[str, Any] = {}

    def load(
        self,
        extension_id: str,
        spec: ExtensionSpec | None,
    ) -> Any | None:
        if spec is None or not spec.path:
            self.log.warning(
                "no extension package path configured for '%s' — skipping",
                extension_id,
            )
            return None
        try:
            from agent_sdk import (
                ExtensionManifest,
                load_extension_instance,
                validate_extension_manifest,
            )
        except ImportError:
            self.log.warning(
                "agent-sdk not installed — cannot load extension '%s'",
                extension_id,
            )
            return None

        package_path = self._resolve_package_path(spec.path)
        try:
            manifest = ExtensionManifest.load(package_path)
            result = validate_extension_manifest(
                manifest,
                core_version=self.core_version,
            )
            if not result.ok:
                self.log.warning(
                    "invalid extension manifest for '%s': %s",
                    extension_id,
                    "; ".join(result.errors),
                )
                return None
            if manifest.id != extension_id:
                self.log.warning(
                    "extension id mismatch for '%s': manifest declares '%s'",
                    extension_id,
                    manifest.id,
                )
                return None
            if manifest.kind.value != spec.kind:
                self.log.warning(
                    "extension kind mismatch for '%s': config=%s manifest=%s",
                    extension_id,
                    spec.kind,
                    manifest.kind.value,
                )
                return None
            instance = load_extension_instance(
                manifest,
                package_path,
                core_version=self.core_version,
                kwargs=self._kwargs(extension_id),
            )
            self.manifests[extension_id] = manifest
            return instance
        except Exception as exc:  # noqa: BLE001
            self.log.warning("failed loading extension '%s': %s", extension_id, exc)
            return None

    def _resolve_package_path(self, value: str) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.root_path / candidate
        return candidate.resolve()

    def _kwargs(self, extension_id: str) -> dict[str, Any]:
        if extension_id in _WORKSPACE_CONFINED:
            return {"workspace_root": self.workspace_path}
        return {}


# Compatibility import for 0.1 callers.
CapabilityLoader = ExtensionLoader
