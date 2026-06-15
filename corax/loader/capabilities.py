"""Capability package loader.

Richer capabilities (filesystem, editor, shell, …) ship as standalone
SDK packages rather than in-tree code. Each package declares a root
``capability.json`` manifest and a ``main.py`` entrypoint. This loader:

1. resolves the package path (relative paths are anchored at the repo root),
2. loads and validates the manifest against the core version,
3. confirms the manifest's declared id matches what config asked for,
4. instantiates the capability through ``agent-sdk``'s ``load_instance``.

``agent-sdk`` is imported **lazily**, inside :meth:`CapabilityLoader.load`,
so the scaffold (menu, config, built-in components) runs on a pure-stdlib
install. The dependency is only required to load real capability packages.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import ProviderSpec

# Capabilities that should be sandboxed to the agent workspace receive it
# as a constructor keyword. Extend this as new workspace-confined tools land.
_WORKSPACE_CONFINED = {"filesystem", "editor"}


class CapabilityLoader:
    """Builds capability instances from standalone SDK packages."""

    def __init__(
        self,
        *,
        root_path: str | Path,
        workspace_path: str | Path,
        core_version: str = "0.1.0",
        log: logging.Logger | None = None,
    ) -> None:
        self.root_path = Path(root_path)
        self.workspace_path = Path(workspace_path)
        self.core_version = core_version
        self.log = log or logging.getLogger("corax.loader")

    def load(self, capability_id: str, spec: ProviderSpec | None) -> Any | None:
        """Return a capability instance for ``capability_id`` or ``None``.

        Never raises: any failure is logged and reported as ``None`` so a
        single broken capability cannot stop the runtime from starting.
        """
        if spec is None or not spec.path:
            self.log.warning(
                "no capability package path configured for '%s' — skipping",
                capability_id,
            )
            return None

        try:
            from agent_sdk import CapabilityManifest, load_instance, validate_manifest
        except ImportError:
            self.log.warning(
                "agent-sdk not installed — cannot load capability package '%s'",
                capability_id,
            )
            return None

        package_path = self._resolve_package_path(spec.path)
        try:
            manifest = CapabilityManifest.load(package_path)
            result = validate_manifest(manifest, core_version=self.core_version)
            if not result.ok:
                self.log.warning(
                    "invalid capability manifest for '%s': %s",
                    capability_id,
                    "; ".join(result.errors),
                )
                return None
            if manifest.id != capability_id:
                self.log.warning(
                    "capability id mismatch for '%s': manifest declares '%s'",
                    capability_id,
                    manifest.id,
                )
                return None
            return load_instance(
                manifest,
                package_path,
                core_version=self.core_version,
                kwargs=self._kwargs(capability_id),
            )
        except Exception as exc:  # noqa: BLE001 - startup should report and continue
            self.log.warning("failed loading capability '%s': %s", capability_id, exc)
            return None

    # -- internals ------------------------------------------------------- #
    def _resolve_package_path(self, value: str) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.root_path / candidate
        return candidate.resolve()

    def _kwargs(self, capability_id: str) -> dict[str, Any]:
        if capability_id in _WORKSPACE_CONFINED:
            return {"workspace_root": self.workspace_path}
        return {}
