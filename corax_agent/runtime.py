"""Corax runtime.

Owns the four registries and populates them from config. Planner, memory and
connectors are still stub-backed; capabilities can now be either local stubs or
standalone SDK packages loaded from their root ``capability.json`` manifests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent_sdk import CapabilityManifest, load_instance, validate_manifest

from .config import AgentConfig
from .registries import (
    CapabilityRegistryAdapter,
    ConnectorRegistry,
    MemoryRegistry,
    ProviderRegistry,
)
from .stubs import CapabilityStub, ConnectorStub, MemoryStub, PlannerStub

# Known stub factories, keyed by the provider id used in agent.yaml.
# Real implementations register additional ids here later.
_PLANNER_FACTORIES: dict[str, Callable[[], Any]] = {"stub": PlannerStub}
_MEMORY_FACTORIES: dict[str, Callable[[], Any]] = {"none": MemoryStub}
_CONNECTOR_FACTORIES: dict[str, Callable[[], Any]] = {"terminal": ConnectorStub}
_CAPABILITY_FACTORIES: dict[str, Callable[[], Any]] = {"stub.echo": CapabilityStub}


@dataclass
class RuntimeStatus:
    """A snapshot of the runtime, safe to print or serialise."""

    running: bool
    started_at: str | None
    uptime_seconds: float
    agent_name: str
    mode: str
    planner_active: str
    memory_active: str
    connectors_active: list[str] = field(default_factory=list)
    capabilities_enabled: list[str] = field(default_factory=list)
    registry_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "uptime_seconds": round(self.uptime_seconds, 3),
            "agent_name": self.agent_name,
            "mode": self.mode,
            "planner_active": self.planner_active,
            "memory_active": self.memory_active,
            "connectors_active": self.connectors_active,
            "capabilities_enabled": self.capabilities_enabled,
            "registry_counts": self.registry_counts,
        }

    def render(self) -> str:
        lines = [
            f"  state          : {'RUNNING' if self.running else 'stopped'}",
            f"  agent / mode   : {self.agent_name} / {self.mode}",
            f"  started_at     : {self.started_at or '-'}",
            f"  uptime         : {self.uptime_seconds:.1f}s",
            f"  planner        : {self.planner_active}",
            f"  memory         : {self.memory_active}",
            f"  connectors     : {', '.join(self.connectors_active) or '-'}",
            f"  capabilities   : {', '.join(self.capabilities_enabled) or '-'}",
            "  registries     : "
            + ", ".join(f"{k}={v}" for k, v in self.registry_counts.items()),
        ]
        return "\n".join(lines)


class CoraxRuntime:
    """Lifecycle owner for the agent's registries."""

    def __init__(
        self,
        config: AgentConfig,
        logger: logging.Logger | None = None,
        *,
        root_path: str | Path | None = None,
        workspace_path: str | Path | None = None,
        core_version: str = "0.1.0",
    ) -> None:
        self.config = config
        self.log = logger or logging.getLogger("corax.runtime")
        self.root_path = Path(root_path or Path.cwd()).resolve()
        self.workspace_path = Path(
            workspace_path or self.root_path / config.runtime.workspace_path
        ).resolve()
        self.core_version = core_version

        self.connectors = ConnectorRegistry()
        self.memory = MemoryRegistry()
        self.providers = ProviderRegistry()
        self.capabilities = CapabilityRegistryAdapter()

        self._running = False
        self._started_at: datetime | None = None

    # -- lifecycle ------------------------------------------------------- #
    async def start(self) -> None:
        if self._running:
            self.log.debug("runtime already running")
            return
        self.log.info("starting runtime")
        self._populate_registries()
        self._running = True
        self._started_at = datetime.now(timezone.utc)
        self.log.info(
            "runtime started: planner=%s memory=%s connectors=%s capabilities=%s",
            self.config.planner.active,
            self.config.memory.active,
            self.config.connectors.active,
            self.config.capabilities.enabled,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self.log.info("stopping runtime")
        self._clear_registries()
        self._running = False
        self._started_at = None

    async def reload_config(self, config: AgentConfig | None = None) -> None:
        """Re-apply config: stop, swap, repopulate."""
        was_running = self._running
        await self.stop()
        if config is not None:
            self.config = config
        if was_running:
            await self.start()

    async def status(self) -> RuntimeStatus:
        return self.snapshot()

    def snapshot(self) -> RuntimeStatus:
        """Synchronous status snapshot (safe to call from the blocking menu)."""
        uptime = 0.0
        started = None
        if self._started_at is not None:
            started = self._started_at.isoformat()
            uptime = (datetime.now(timezone.utc) - self._started_at).total_seconds()
        return RuntimeStatus(
            running=self._running,
            started_at=started,
            uptime_seconds=uptime,
            agent_name=self.config.agent.name,
            mode=self.config.agent.mode,
            planner_active=self.config.planner.active,
            memory_active=self.config.memory.active,
            connectors_active=list(self.config.connectors.active),
            capabilities_enabled=list(self.config.capabilities.enabled),
            registry_counts={
                "providers": len(self.providers),
                "memory": len(self.memory),
                "connectors": len(self.connectors),
                "capabilities": len(self.capabilities),
            },
        )

    @property
    def running(self) -> bool:
        return self._running

    # -- internals ------------------------------------------------------- #
    def _populate_registries(self) -> None:
        self._clear_registries()

        # Planner (single active provider -> ProviderRegistry).
        planner_id = self.config.planner.active
        item = self._build(_PLANNER_FACTORIES, planner_id, "planner")
        if item is not None:
            self.providers.register(planner_id, item)

        # Memory (single active backend).
        memory_id = self.config.memory.active
        item = self._build(_MEMORY_FACTORIES, memory_id, "memory")
        if item is not None:
            self.memory.register(memory_id, item)

        # Connectors (list of active ids).
        for cid in self.config.connectors.active:
            spec = self.config.connectors.providers.get(cid)
            if spec is not None and not spec.enabled:
                continue
            item = self._build(_CONNECTOR_FACTORIES, cid, "connector")
            if item is not None:
                self.connectors.register(cid, item)

        # Capabilities (list of enabled ids).
        for cap_id in self.config.capabilities.enabled:
            spec = self.config.capabilities.available.get(cap_id)
            if spec is not None and not spec.enabled:
                continue
            item = self._build_capability(cap_id)
            if item is not None:
                self.capabilities.register(cap_id, item)

    def _build(self, factories: dict[str, Callable[[], Any]], id: str, role: str) -> Any:
        factory = factories.get(id)
        if factory is None:
            self.log.warning(
                "no stub for %s '%s' — skipping (will be provided by a real module later)",
                role,
                id,
            )
            return None
        return factory()

    def _build_capability(self, id: str) -> Any:
        factory = _CAPABILITY_FACTORIES.get(id)
        if factory is not None:
            return factory()

        spec = self.config.capabilities.available.get(id)
        if spec is None or not spec.path:
            self.log.warning(
                "no capability package path configured for '%s' — skipping",
                id,
            )
            return None

        package_path = self._resolve_package_path(spec.path)
        try:
            manifest = CapabilityManifest.load(package_path)
            result = validate_manifest(manifest, core_version=self.core_version)
            if not result.ok:
                self.log.warning(
                    "invalid capability manifest for '%s': %s",
                    id,
                    "; ".join(result.errors),
                )
                return None
            if manifest.id != id:
                self.log.warning(
                    "capability id mismatch for '%s': manifest declares '%s'",
                    id,
                    manifest.id,
                )
                return None
            kwargs = self._capability_kwargs(id)
            return load_instance(
                manifest,
                package_path,
                core_version=self.core_version,
                kwargs=kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - startup should report and continue
            self.log.warning("failed loading capability '%s': %s", id, exc)
            return None

    def _resolve_package_path(self, value: str) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.root_path / candidate
        return candidate.resolve()

    def _capability_kwargs(self, id: str) -> dict[str, Any]:
        if id in {"filesystem", "editor"}:
            return {"workspace_root": self.workspace_path}
        return {}

    def _clear_registries(self) -> None:
        self.connectors.clear()
        self.memory.clear()
        self.providers.clear()
        self.capabilities.clear()
