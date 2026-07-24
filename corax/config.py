"""Configuration model and (de)serialisation for Corax Agent.

The config is a tree of plain dataclasses mirroring ``corax.yaml``.
Storage format is chosen by file extension:

* ``.yaml`` / ``.yml`` -> YAML (via :mod:`corax.yaml_lite`)
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

from . import yaml_lite

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
VALID_SECURITY_MODES = {"ask", "auto", "full"}
LEGACY_SECURITY_MODES = {
    "normal": "ask",
    "strict": "ask",
    "paranoid": "ask",
}
VALID_EXTENSION_KINDS = {
    "tool",
    "channel_connector",
    "model_provider",
    "memory_provider",
    "policy_provider",
    "runtime_service",
    "storage_provider",
    "observability",
    "adapter",
}
REQUIRED_SECTIONS = (
    "agent",
    "runtime",
    "extensions",
    "security",
    "limits",
    "ui",
    "llm",
    "telegram",
    "websearch",
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
    path: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProviderSpec":
        return cls(
            enabled=bool(data.get("enabled", True)),
            type=str(data.get("type", "provider")),
            description=str(data.get("description", "")),
            path=str(data.get("path", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "enabled": self.enabled,
            "type": self.type,
            "description": self.description,
        }
        if self.path:
            data["path"] = self.path
        return data


@dataclass
class ExtensionSpec:
    """Configuration for one installable typed extension."""

    kind: str = "tool"
    enabled: bool = True
    description: str = ""
    path: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtensionSpec":
        # ``type`` is accepted for pre-0.2 configuration imports.
        return cls(
            kind=str(data.get("kind", data.get("type", "tool"))),
            enabled=bool(data.get("enabled", True)),
            description=str(data.get("description", "")),
            path=str(data.get("path", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "enabled": self.enabled,
            "description": self.description,
        }
        if self.path:
            data["path"] = self.path
        return data

    def as_legacy(self) -> ProviderSpec:
        return ProviderSpec(
            enabled=self.enabled,
            type=self.kind,
            description=self.description,
            path=self.path,
        )


@dataclass
class ExtensionsConfig:
    """Single extension catalogue plus activation and role bindings."""

    active: dict[str, list[str]] = field(default_factory=dict)
    bindings: dict[str, str] = field(default_factory=dict)
    available: dict[str, ExtensionSpec] = field(default_factory=dict)

    def active_for(self, kind: str) -> list[str]:
        return list(self.active.get(kind, ()))


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
    mode: str = "ask"
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
class LLMConfig:
    """Setup for the local LLM connector capability (``llm.local``).

    Edited in the runtime menu and exported to ``CORAX_LLM_*`` environment
    variables when the runtime starts, so the standalone connector picks up the
    operator's choices without any per-capability wiring. ``enable_image`` /
    ``enable_video`` are the multimodality selection; text input is always on.
    """

    base_url: str = "http://192.168.0.10:8000/v1"
    model: str = "google/gemma-4-12B-it"
    enable_image: bool = False
    enable_video: bool = False


@dataclass
class TelegramConfig:
    """Setup for the Telegram connector capability (``telegram.connector``).

    Edited in the runtime menu and exported to ``CORAX_TELEGRAM_*`` when the
    runtime starts. The bot **token is never stored here** — it is read from the
    ``CORAX_TELEGRAM_BOT_TOKEN`` environment variable. ``allowed_chats`` is a
    comma-separated allow-list (empty means allow any chat).
    """

    base_url: str = "https://api.telegram.org"
    allowed_chats: str = ""


@dataclass
class WebSearchConfig:
    """Setup for the web-search tool capability (``web.search``).

    Edited in the runtime menu and exported to ``CORAX_WEBSEARCH_*`` when the
    runtime starts, so the standalone SearXNG tool picks up the operator's
    endpoint and default query knobs without any per-capability wiring.
    ``base_url`` is the self-hosted SearXNG instance (must be a local/private
    address). ``engines`` / ``language`` / ``safesearch`` are optional defaults
    (empty means "unset" and is not exported). The optional reverse-proxy token
    is **never stored here** — it is read from ``CORAX_WEBSEARCH_TOKEN``.
    """

    base_url: str = "http://192.168.0.14:8080"
    engines: str = ""
    language: str = ""
    safesearch: str = ""


@dataclass
class AgentConfig:
    agent: AgentMeta = field(default_factory=AgentMeta)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    connectors: ConnectorsConfig = field(default_factory=ConnectorsConfig)
    capabilities: CapabilitiesConfig = field(default_factory=CapabilitiesConfig)
    extensions: ExtensionsConfig = field(default_factory=ExtensionsConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    websearch: WebSearchConfig = field(default_factory=WebSearchConfig)

    def to_dict(self) -> dict[str, Any]:
        return config_to_dict(self)

    def refresh_legacy_views(self) -> None:
        """Populate deprecated 0.1 config views from the canonical catalogue."""

        self.planner = PlannerConfig(
            active=self.extensions.bindings.get("planner", ""),
            providers={
                extension_id: spec.as_legacy()
                for extension_id, spec in self.extensions.available.items()
                if spec.kind == "model_provider"
            },
        )
        memory_active = self.extensions.bindings.get("memory", "")
        self.memory = MemoryConfig(
            active=memory_active,
            providers={
                extension_id: spec.as_legacy()
                for extension_id, spec in self.extensions.available.items()
                if spec.kind == "memory_provider"
            },
        )
        self.connectors = ConnectorsConfig(
            active=self.extensions.active_for("channel_connector"),
            providers={
                extension_id: spec.as_legacy()
                for extension_id, spec in self.extensions.available.items()
                if spec.kind == "channel_connector"
            },
        )
        self.capabilities = CapabilitiesConfig(
            enabled=self.extensions.active_for("tool"),
            available={
                extension_id: spec.as_legacy()
                for extension_id, spec in self.extensions.available.items()
                if spec.kind == "tool"
            },
        )


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
def default_config() -> AgentConfig:
    """Return the in-memory default configuration (matches agent.yaml)."""
    config = AgentConfig(
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
            enabled=[
                "echo",
                "filesystem",
                "editor",
                "shell",
                "gateway",
                "llm.local",
                "telegram.connector",
                "web.search",
            ],
            available={
                "echo": ProviderSpec(
                    enabled=True, type="tool",
                    description="Built-in echo capability",
                ),
                "filesystem": ProviderSpec(
                    enabled=True,
                    type="tool",
                    description="Workspace-confined filesystem capability",
                    path="../corax-filesystem-capability",
                ),
                "editor": ProviderSpec(
                    enabled=True,
                    type="tool",
                    description="Workspace-confined text editor capability",
                    path="../corax-editor-capability",
                ),
                "shell": ProviderSpec(
                    enabled=True,
                    type="tool",
                    description="Guarded local shell command capability",
                    path="../corax-shell-capability",
                ),
                "gateway": ProviderSpec(
                    enabled=True,
                    type="tool",
                    description="Channel-agnostic gateway policy and session context",
                    path="../corax-gateway-capability",
                ),
                "llm.local": ProviderSpec(
                    enabled=True,
                    type="connector",
                    description="Local Spark LLM connector (text + optional image/video)",
                    path="../corax-llm-local-connector",
                ),
                "telegram.connector": ProviderSpec(
                    enabled=True,
                    type="connector",
                    description="Telegram chat connector (streaming, commands, HTML formatting)",
                    path="../corax-telegram-connector",
                ),
                "web.search": ProviderSpec(
                    enabled=True,
                    type="tool",
                    description="Web search via a self-hosted SearXNG instance",
                    path="../corax-web-search-capability",
                ),
            },
        ),
        extensions=ExtensionsConfig(
            active={
                "tool": [
                    "echo",
                    "filesystem",
                    "editor",
                    "shell",
                    "web.search",
                ],
                "channel_connector": [
                    "console.connector",
                    "telegram.connector",
                ],
                "model_provider": ["stub", "llm.local"],
                "memory_provider": ["memory.none"],
                "policy_provider": ["security.policy"],
                "runtime_service": [
                    "gateway",
                    "memory.loop",
                    "context.manager",
                    "mcp.manager",
                    "skills.runtime",
                    "hooks.runtime",
                    "subagents.orchestrator",
                ],
                "storage_provider": ["state.file"],
            },
            bindings={
                "planner": "stub",
                "primary_model": "llm.local",
                "memory": "memory.none",
                "memory_loop": "memory.loop",
                "policy": "security.policy",
                "state": "state.file",
                "context": "context.manager",
                "mcp": "mcp.manager",
                "skills": "skills.runtime",
                "hooks": "hooks.runtime",
                "subagents": "subagents.orchestrator",
            },
            available={
                "stub": ExtensionSpec(
                    kind="model_provider",
                    description="Built-in deterministic planner",
                ),
                "memory.none": ExtensionSpec(
                    kind="memory_provider",
                    description="Built-in no-op memory provider",
                ),
                "memory.uam": ExtensionSpec(
                    kind="memory_provider",
                    enabled=False,
                    description="Universal Agent Memory provider",
                    path="../universal-agent-memory/agent-integrations/corax",
                ),
                "memory.mnemonic-vault": ExtensionSpec(
                    kind="memory_provider",
                    enabled=False,
                    description="Mnemonic Vault file-first memory provider",
                    path="../mnemonic-vault/integrations/corax",
                ),
                "memory.loop": ExtensionSpec(
                    kind="runtime_service",
                    description="Bounded recall and privacy-aware retention loop",
                    path="../corax-memory-loop",
                ),
                "context.manager": ExtensionSpec(
                    kind="runtime_service",
                    description="Deterministic bounded context compaction",
                    path="../corax-context-manager",
                ),
                "mcp.manager": ExtensionSpec(
                    kind="runtime_service",
                    description="Official-SDK MCP client and tool bridge",
                    path="../corax-mcp-manager",
                ),
                "skills.runtime": ExtensionSpec(
                    kind="runtime_service",
                    description="Portable progressive Agent Skills loader",
                    path="../corax-skills-runtime",
                ),
                "hooks.runtime": ExtensionSpec(
                    kind="runtime_service",
                    description="Consent-gated subprocess lifecycle hooks",
                    path="../corax-hooks-runtime",
                ),
                "subagents.orchestrator": ExtensionSpec(
                    kind="runtime_service",
                    description="Bounded parallel leaf-subagent delegation",
                    path="../corax-subagents",
                ),
                "terminal": ExtensionSpec(
                    kind="channel_connector",
                    description="Built-in terminal connector",
                ),
                "console.connector": ExtensionSpec(
                    kind="channel_connector",
                    description="Interactive local console channel",
                    path="../corax-console",
                ),
                "echo": ExtensionSpec(
                    kind="tool",
                    description="Built-in echo tool",
                ),
                "filesystem": ExtensionSpec(
                    kind="tool",
                    description="Workspace-confined filesystem tool",
                    path="../corax-filesystem-capability",
                ),
                "editor": ExtensionSpec(
                    kind="tool",
                    description="Workspace-confined editor tool",
                    path="../corax-editor-capability",
                ),
                "shell": ExtensionSpec(
                    kind="tool",
                    description="Guarded local shell tool",
                    path="../corax-shell-capability",
                ),
                "web.search": ExtensionSpec(
                    kind="tool",
                    description="SearXNG web search tool",
                    path="../corax-web-search-capability",
                ),
                "gateway": ExtensionSpec(
                    kind="runtime_service",
                    description="Channel-neutral gateway/session service",
                    path="../corax-gateway-capability",
                ),
                "llm.local": ExtensionSpec(
                    kind="model_provider",
                    description="Local OpenAI-compatible model provider",
                    path="../corax-llm-local-connector",
                ),
                "telegram.connector": ExtensionSpec(
                    kind="channel_connector",
                    description="Telegram channel connector",
                    path="../corax-telegram-connector",
                ),
                "security.policy": ExtensionSpec(
                    kind="policy_provider",
                    description="Three-mode authorization policy",
                    path="../corax-security-policy",
                ),
                "state.file": ExtensionSpec(
                    kind="storage_provider",
                    description="Atomic local JSON checkpoint storage",
                    path="../corax-state-store",
                ),
            },
        ),
        security=SecurityConfig(
            mode="ask",
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
        llm=LLMConfig(
            base_url="http://192.168.0.10:8000/v1",
            model="google/gemma-4-12B-it",
            enable_image=False,
            enable_video=False,
        ),
        telegram=TelegramConfig(
            base_url="https://api.telegram.org",
            allowed_chats="",
        ),
        websearch=WebSearchConfig(
            base_url="http://192.168.0.14:8080",
            engines="",
            language="",
            safesearch="",
        ),
    )
    config.refresh_legacy_views()
    return config


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def _providers_to_dict(providers: dict[str, ProviderSpec]) -> dict[str, Any]:
    return {pid: spec.to_dict() for pid, spec in providers.items()}


def _providers_from_dict(data: dict[str, Any]) -> dict[str, ProviderSpec]:
    return {pid: ProviderSpec.from_dict(spec or {}) for pid, spec in (data or {}).items()}


def _extensions_to_dict(config: ExtensionsConfig) -> dict[str, Any]:
    return {
        "active": {
            kind: list(extension_ids)
            for kind, extension_ids in sorted(config.active.items())
        },
        "bindings": dict(sorted(config.bindings.items())),
        "available": {
            extension_id: spec.to_dict()
            for extension_id, spec in sorted(config.available.items())
        },
    }


def _extensions_from_dict(data: dict[str, Any]) -> ExtensionsConfig:
    return ExtensionsConfig(
        active={
            str(kind): list(extension_ids or [])
            for kind, extension_ids in (data.get("active", {}) or {}).items()
        },
        bindings={
            str(role): str(extension_id)
            for role, extension_id in (data.get("bindings", {}) or {}).items()
        },
        available={
            extension_id: ExtensionSpec.from_dict(spec or {})
            for extension_id, spec in (data.get("available", {}) or {}).items()
        },
    )


def _extensions_from_legacy(data: dict[str, Any]) -> ExtensionsConfig:
    """Import a 0.1 config without preserving its incorrect role grouping."""

    planner = data.get("planner", {}) or {}
    memory = data.get("memory", {}) or {}
    connectors = data.get("connectors", {}) or {}
    capabilities = data.get("capabilities", {}) or {}
    available: dict[str, ExtensionSpec] = {}

    for extension_id, raw in (planner.get("providers", {}) or {}).items():
        spec = ExtensionSpec.from_dict(raw or {})
        spec.kind = "model_provider"
        available[extension_id] = spec
    for extension_id, raw in (memory.get("providers", {}) or {}).items():
        spec = ExtensionSpec.from_dict(raw or {})
        spec.kind = "memory_provider"
        canonical_id = (
            "memory.none" if extension_id == "none" else extension_id
        )
        available[canonical_id] = spec
    for extension_id, raw in (connectors.get("providers", {}) or {}).items():
        spec = ExtensionSpec.from_dict(raw or {})
        spec.kind = "channel_connector"
        available[extension_id] = spec

    legacy_kind_map = {
        "tool": "tool",
        "connector": "channel_connector",
        "memory": "memory_provider",
        "planner": "model_provider",
        "service": "runtime_service",
        "adapter": "adapter",
    }
    for extension_id, raw in (capabilities.get("available", {}) or {}).items():
        spec = ExtensionSpec.from_dict(raw or {})
        spec.kind = legacy_kind_map.get(spec.kind, spec.kind)
        # Known 0.1 packages had misleading connector/tool labels.
        if extension_id == "llm.local":
            spec.kind = "model_provider"
        elif extension_id == "telegram.connector":
            spec.kind = "channel_connector"
        elif extension_id == "gateway":
            spec.kind = "runtime_service"
        available[extension_id] = spec

    enabled = list(capabilities.get("enabled", []) or [])
    active: dict[str, list[str]] = {}
    for extension_id, spec in available.items():
        should_activate = (
            extension_id in enabled
            or extension_id in (connectors.get("active", []) or [])
            or extension_id == planner.get("active")
            or extension_id == memory.get("active")
            or (
                extension_id == "memory.none"
                and memory.get("active") == "none"
            )
        )
        if should_activate and spec.enabled:
            active.setdefault(spec.kind, []).append(extension_id)
    return ExtensionsConfig(
        active=active,
        bindings={
            "planner": str(planner.get("active", "stub")),
            "primary_model": "llm.local",
            "memory": (
                "memory.none"
                if memory.get("active", "none") == "none"
                else str(memory.get("active"))
            ),
        },
        available=available,
    )


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
        "extensions": _extensions_to_dict(config.extensions),
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
        "llm": {
            "base_url": config.llm.base_url,
            "model": config.llm.model,
            "enable_image": config.llm.enable_image,
            "enable_video": config.llm.enable_video,
        },
        "telegram": {
            "base_url": config.telegram.base_url,
            "allowed_chats": config.telegram.allowed_chats,
        },
        "websearch": {
            "base_url": config.websearch.base_url,
            "engines": config.websearch.engines,
            "language": config.websearch.language,
            "safesearch": config.websearch.safesearch,
        },
    }


def config_from_dict(data: dict[str, Any]) -> AgentConfig:
    data = data or {}
    agent = data.get("agent", {}) or {}
    runtime = data.get("runtime", {}) or {}
    extensions = (
        _extensions_from_dict(data.get("extensions", {}) or {})
        if "extensions" in data
        else _extensions_from_legacy(data)
    )
    security = data.get("security", {}) or {}
    limits = data.get("limits", {}) or {}
    ui = data.get("ui", {}) or {}
    llm = data.get("llm", {}) or {}
    telegram = data.get("telegram", {}) or {}
    websearch = data.get("websearch", {}) or {}

    defaults = default_config()
    config = AgentConfig(
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
        extensions=extensions,
        security=SecurityConfig(
            mode=LEGACY_SECURITY_MODES.get(
                str(security.get("mode", defaults.security.mode)),
                str(security.get("mode", defaults.security.mode)),
            ),
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
        llm=LLMConfig(
            base_url=str(llm.get("base_url", defaults.llm.base_url)),
            model=str(llm.get("model", defaults.llm.model)),
            enable_image=bool(llm.get("enable_image", defaults.llm.enable_image)),
            enable_video=bool(llm.get("enable_video", defaults.llm.enable_video)),
        ),
        telegram=TelegramConfig(
            base_url=str(telegram.get("base_url", defaults.telegram.base_url)),
            allowed_chats=str(telegram.get("allowed_chats", defaults.telegram.allowed_chats)),
        ),
        websearch=WebSearchConfig(
            base_url=str(websearch.get("base_url", defaults.websearch.base_url)),
            engines=str(websearch.get("engines", defaults.websearch.engines)),
            language=str(websearch.get("language", defaults.websearch.language)),
            safesearch=str(websearch.get("safesearch", defaults.websearch.safesearch)),
        ),
    )
    config.refresh_legacy_views()
    return config


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
        if not yaml_lite.HAS_PYYAML and not text.strip():
            data: Any = {}
        else:
            data = yaml_lite.loads(text)
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
        text = yaml_lite.dumps(data)
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

    for extension_id, spec in config.extensions.available.items():
        if spec.kind not in VALID_EXTENSION_KINDS:
            errors.append(
                f"extensions.available.{extension_id}.kind "
                f"{spec.kind!r} is invalid"
            )

    active_ids: set[str] = set()
    for kind, extension_ids in config.extensions.active.items():
        if kind not in VALID_EXTENSION_KINDS:
            errors.append(f"extensions.active kind {kind!r} is invalid")
        for extension_id in extension_ids:
            spec = config.extensions.available.get(extension_id)
            if spec is None:
                errors.append(
                    f"extensions.active.{kind} references unknown "
                    f"{extension_id!r}"
                )
                continue
            if spec.kind != kind:
                errors.append(
                    f"extensions.active.{kind} contains {extension_id!r}, "
                    f"declared as {spec.kind!r}"
                )
            if not spec.enabled:
                errors.append(f"active extension {extension_id!r} is disabled")
            if extension_id in active_ids:
                errors.append(
                    f"extension {extension_id!r} is active in multiple roles"
                )
            active_ids.add(extension_id)

    binding_kinds = {
        "planner": "model_provider",
        "primary_model": "model_provider",
        "memory": "memory_provider",
        "memory_loop": "runtime_service",
        "policy": "policy_provider",
        "state": "storage_provider",
        "context": "runtime_service",
        "mcp": "runtime_service",
        "skills": "runtime_service",
        "hooks": "runtime_service",
        "subagents": "runtime_service",
    }
    for role, extension_id in config.extensions.bindings.items():
        spec = config.extensions.available.get(extension_id)
        if spec is None:
            errors.append(
                f"extensions.bindings.{role} references unknown "
                f"{extension_id!r}"
            )
            continue
        expected_kind = binding_kinds.get(role)
        if expected_kind is not None and spec.kind != expected_kind:
            errors.append(
                f"extensions.bindings.{role} requires {expected_kind}, "
                f"got {spec.kind}"
            )
        if extension_id not in active_ids:
            errors.append(
                f"extensions.bindings.{role} references inactive "
                f"{extension_id!r}"
            )

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
