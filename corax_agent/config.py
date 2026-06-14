"""Configuration model and (de)serialisation for Corax Agent.

The config is a tree of plain dataclasses mirroring ``agent.yaml``.
Storage format is chosen by file extension:

* ``.yaml`` / ``.yml`` -> YAML (via :mod:`corax_agent._yaml`)
* ``.json``            -> JSON (stdlib)

Public API:
    load_config(path)            -> AgentConfig
    save_config(config, path)    -> None
    create_default_config(path)  -> AgentConfig
    validate_config(config)      -> list[str]
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _yaml

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
VALID_SECURITY_MODES = {"normal", "strict", "paranoid"}
REQUIRED_SECTIONS = (
    "agent",
    "runtime",
    "planner",
    "memory",
    "connectors",
    "capabilities",
    "security",
    "limits",
    "ui",
)


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class AgentMeta:
    name: str = "corax"
    profile: str = "default"
    mode: str = "local"
    first_run: bool = True


@dataclass
class RuntimeConfig:
    autostart: bool = False
    log_level: str = "INFO"
    workspace_path: str = "./workspace"
    data_path: str = "./data"
    logs_path: str = "./logs"


@dataclass
class ProviderSpec:
    enabled: bool = True
    type: str = "provider"
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProviderSpec":
        return cls(
            enabled=bool(data.get("enabled", True)),
            type=str(data.get("type", "provider")),
            description=str(data.get("description", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "type": self.type, "description": self.description}


@dataclass
class PlannerConfig:
    active: str = "stub"
    providers: dict[str, ProviderSpec] = field(default_factory=dict)


@dataclass
class MemoryConfig:
    active: str = "none"
    providers: dict[str, ProviderSpec] = field(default_factory=dict)


@dataclass
class ConnectorsConfig:
    active: list[str] = field(default_factory=list)
    providers: dict[str, ProviderSpec] = field(default_factory=dict)


@dataclass
class CapabilitiesConfig:
    enabled: list[str] = field(default_factory=list)
    available: dict[str, ProviderSpec] = field(default_factory=dict)


@dataclass
class SecurityConfig:
    mode: str = "normal"
    core_readonly: bool = True
    allow_shell: bool = False
    allow_file_write: bool = False
    blocked_paths: list[str] = field(default_factory=list)


@dataclass
class LimitsConfig:
    max_parallel_tasks: int = 4
    max_plan_tasks: int = 30
    max_tasks_per_correlation: int = 50
    task_timeout_seconds: int = 60
    max_payload_mb: int = 20


@dataclass
class UIConfig:
    theme: str = "terminal"
    mascot: str = "corax"
    show_banner: bool = True


@dataclass
class AgentConfig:
    agent: AgentMeta = field(default_factory=AgentMeta)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    connectors: ConnectorsConfig = field(default_factory=ConnectorsConfig)
    capabilities: CapabilitiesConfig = field(default_factory=CapabilitiesConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    def to_dict(self) -> dict[str, Any]:
        return config_to_dict(self)


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
def default_config() -> AgentConfig:
    """Return the in-memory default configuration (matches agent.yaml)."""
    return AgentConfig(
        agent=AgentMeta(name="corax", profile="default", mode="local", first_run=True),
        runtime=RuntimeConfig(
            autostart=False,
            log_level="INFO",
            workspace_path="./workspace",
            data_path="./data",
            logs_path="./logs",
        ),
        planner=PlannerConfig(
            active="stub",
            providers={
                "stub": ProviderSpec(
                    enabled=True, type="planner",
                    description="Local stub planner for scaffold testing",
                )
            },
        ),
        memory=MemoryConfig(
            active="none",
            providers={
                "none": ProviderSpec(
                    enabled=True, type="memory",
                    description="No persistent memory yet",
                )
            },
        ),
        connectors=ConnectorsConfig(
            active=["terminal"],
            providers={
                "terminal": ProviderSpec(
                    enabled=True, type="connector",
                    description="Terminal connector placeholder",
                )
            },
        ),
        capabilities=CapabilitiesConfig(
            enabled=["stub.echo"],
            available={
                "stub.echo": ProviderSpec(
                    enabled=True, type="tool",
                    description="Stub echo capability",
                )
            },
        ),
        security=SecurityConfig(
            mode="normal",
            core_readonly=True,
            allow_shell=False,
            allow_file_write=False,
            blocked_paths=["../corax-core", "../corax-sdk", "~/.ssh", ".env"],
        ),
        limits=LimitsConfig(
            max_parallel_tasks=4,
            max_plan_tasks=30,
            max_tasks_per_correlation=50,
            task_timeout_seconds=60,
            max_payload_mb=20,
        ),
        ui=UIConfig(theme="terminal", mascot="corax", show_banner=True),
    )


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def _providers_to_dict(providers: dict[str, ProviderSpec]) -> dict[str, Any]:
    return {pid: spec.to_dict() for pid, spec in providers.items()}


def _providers_from_dict(data: dict[str, Any]) -> dict[str, ProviderSpec]:
    return {pid: ProviderSpec.from_dict(spec or {}) for pid, spec in (data or {}).items()}


def config_to_dict(config: AgentConfig) -> dict[str, Any]:
    return {
        "agent": {
            "name": config.agent.name,
            "profile": config.agent.profile,
            "mode": config.agent.mode,
            "first_run": config.agent.first_run,
        },
        "runtime": {
            "autostart": config.runtime.autostart,
            "log_level": config.runtime.log_level,
            "workspace_path": config.runtime.workspace_path,
            "data_path": config.runtime.data_path,
            "logs_path": config.runtime.logs_path,
        },
        "planner": {
            "active": config.planner.active,
            "providers": _providers_to_dict(config.planner.providers),
        },
        "memory": {
            "active": config.memory.active,
            "providers": _providers_to_dict(config.memory.providers),
        },
        "connectors": {
            "active": list(config.connectors.active),
            "providers": _providers_to_dict(config.connectors.providers),
        },
        "capabilities": {
            "enabled": list(config.capabilities.enabled),
            "available": _providers_to_dict(config.capabilities.available),
        },
        "security": {
            "mode": config.security.mode,
            "core_readonly": config.security.core_readonly,
            "allow_shell": config.security.allow_shell,
            "allow_file_write": config.security.allow_file_write,
            "blocked_paths": list(config.security.blocked_paths),
        },
        "limits": {
            "max_parallel_tasks": config.limits.max_parallel_tasks,
            "max_plan_tasks": config.limits.max_plan_tasks,
            "max_tasks_per_correlation": config.limits.max_tasks_per_correlation,
            "task_timeout_seconds": config.limits.task_timeout_seconds,
            "max_payload_mb": config.limits.max_payload_mb,
        },
        "ui": {
            "theme": config.ui.theme,
            "mascot": config.ui.mascot,
            "show_banner": config.ui.show_banner,
        },
    }


def config_from_dict(data: dict[str, Any]) -> AgentConfig:
    data = data or {}
    agent = data.get("agent", {}) or {}
    runtime = data.get("runtime", {}) or {}
    planner = data.get("planner", {}) or {}
    memory = data.get("memory", {}) or {}
    connectors = data.get("connectors", {}) or {}
    capabilities = data.get("capabilities", {}) or {}
    security = data.get("security", {}) or {}
    limits = data.get("limits", {}) or {}
    ui = data.get("ui", {}) or {}

    defaults = default_config()
    return AgentConfig(
        agent=AgentMeta(
            name=agent.get("name", defaults.agent.name),
            profile=agent.get("profile", defaults.agent.profile),
            mode=agent.get("mode", defaults.agent.mode),
            first_run=bool(agent.get("first_run", defaults.agent.first_run)),
        ),
        runtime=RuntimeConfig(
            autostart=bool(runtime.get("autostart", defaults.runtime.autostart)),
            log_level=str(runtime.get("log_level", defaults.runtime.log_level)),
            workspace_path=str(runtime.get("workspace_path", defaults.runtime.workspace_path)),
            data_path=str(runtime.get("data_path", defaults.runtime.data_path)),
            logs_path=str(runtime.get("logs_path", defaults.runtime.logs_path)),
        ),
        planner=PlannerConfig(
            active=str(planner.get("active", defaults.planner.active)),
            providers=_providers_from_dict(planner.get("providers", {})),
        ),
        memory=MemoryConfig(
            active=str(memory.get("active", defaults.memory.active)),
            providers=_providers_from_dict(memory.get("providers", {})),
        ),
        connectors=ConnectorsConfig(
            active=list(connectors.get("active", []) or []),
            providers=_providers_from_dict(connectors.get("providers", {})),
        ),
        capabilities=CapabilitiesConfig(
            enabled=list(capabilities.get("enabled", []) or []),
            available=_providers_from_dict(capabilities.get("available", {})),
        ),
        security=SecurityConfig(
            mode=str(security.get("mode", defaults.security.mode)),
            core_readonly=bool(security.get("core_readonly", defaults.security.core_readonly)),
            allow_shell=bool(security.get("allow_shell", defaults.security.allow_shell)),
            allow_file_write=bool(security.get("allow_file_write", defaults.security.allow_file_write)),
            blocked_paths=list(security.get("blocked_paths", []) or []),
        ),
        limits=LimitsConfig(
            max_parallel_tasks=int(limits.get("max_parallel_tasks", defaults.limits.max_parallel_tasks)),
            max_plan_tasks=int(limits.get("max_plan_tasks", defaults.limits.max_plan_tasks)),
            max_tasks_per_correlation=int(
                limits.get("max_tasks_per_correlation", defaults.limits.max_tasks_per_correlation)
            ),
            task_timeout_seconds=int(limits.get("task_timeout_seconds", defaults.limits.task_timeout_seconds)),
            max_payload_mb=int(limits.get("max_payload_mb", defaults.limits.max_payload_mb)),
        ),
        ui=UIConfig(
            theme=str(ui.get("theme", defaults.ui.theme)),
            mascot=str(ui.get("mascot", defaults.ui.mascot)),
            show_banner=bool(ui.get("show_banner", defaults.ui.show_banner)),
        ),
    )


# --------------------------------------------------------------------------- #
# File I/O
# --------------------------------------------------------------------------- #
def _is_yaml(path: Path) -> bool:
    return path.suffix.lower() in (".yaml", ".yml")


def load_config(path: Path) -> AgentConfig:
    """Load configuration from ``path`` (YAML or JSON)."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if _is_yaml(path):
        if not _yaml.HAS_PYYAML and not text.strip():
            data: Any = {}
        else:
            data = _yaml.loads(text)
    else:
        data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a mapping, got {type(data).__name__}")
    return config_from_dict(data)


def save_config(config: AgentConfig, path: Path) -> None:
    """Persist ``config`` to ``path`` (format chosen by extension)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = config_to_dict(config)
    if _is_yaml(path):
        text = _yaml.dumps(data)
    else:
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")


def create_default_config(path: Path) -> AgentConfig:
    """Create and persist a default config at ``path``, returning it."""
    config = default_config()
    save_config(config, path)
    return config


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_config(config: AgentConfig) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid)."""
    errors: list[str] = []

    # Required sections are guaranteed by dataclasses; we re-check references.
    for section in REQUIRED_SECTIONS:
        if not hasattr(config, section):
            errors.append(f"missing required section: {section}")

    if config.runtime.log_level not in VALID_LOG_LEVELS:
        errors.append(
            f"runtime.log_level '{config.runtime.log_level}' is invalid "
            f"(expected one of {sorted(VALID_LOG_LEVELS)})"
        )

    if config.security.mode not in VALID_SECURITY_MODES:
        errors.append(
            f"security.mode '{config.security.mode}' is invalid "
            f"(expected one of {sorted(VALID_SECURITY_MODES)})"
        )

    # Active planner must exist and be enabled.
    if config.planner.active not in config.planner.providers:
        errors.append(f"planner.active '{config.planner.active}' has no matching provider")
    elif not config.planner.providers[config.planner.active].enabled:
        errors.append(f"planner.active '{config.planner.active}' is disabled")

    # Active memory must exist and be enabled.
    if config.memory.active not in config.memory.providers:
        errors.append(f"memory.active '{config.memory.active}' has no matching provider")
    elif not config.memory.providers[config.memory.active].enabled:
        errors.append(f"memory.active '{config.memory.active}' is disabled")

    # Active connectors must exist.
    for cid in config.connectors.active:
        if cid not in config.connectors.providers:
            errors.append(f"connectors.active '{cid}' has no matching provider")
        elif not config.connectors.providers[cid].enabled:
            errors.append(f"connectors.active '{cid}' is disabled")

    # Enabled capabilities must exist.
    for cap in config.capabilities.enabled:
        if cap not in config.capabilities.available:
            errors.append(f"capabilities.enabled '{cap}' is not available")
        elif not config.capabilities.available[cap].enabled:
            errors.append(f"capabilities.enabled '{cap}' is disabled")

    # Limits must be positive.
    for name in (
        "max_parallel_tasks",
        "max_plan_tasks",
        "max_tasks_per_correlation",
        "task_timeout_seconds",
        "max_payload_mb",
    ):
        value = getattr(config.limits, name)
        if not isinstance(value, int) or value <= 0:
            errors.append(f"limits.{name} must be a positive integer, got {value!r}")

    return errors
