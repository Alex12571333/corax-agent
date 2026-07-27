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
    "prompts",
    "tool_routing",
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

@dataclass
class ExtensionsConfig:
    """Single extension catalogue plus activation and role bindings."""

    active: dict[str, list[str]] = field(default_factory=dict)
    bindings: dict[str, str] = field(default_factory=dict)
    available: dict[str, ExtensionSpec] = field(default_factory=dict)

    def active_for(self, kind: str) -> list[str]:
        return list(self.active.get(kind, ()))


@dataclass
class SecurityConfig:
    mode: str = "ask"
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
    theme: str = "corax-hologram"
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
class PromptsConfig:
    """Layered prompt assembly and operator-editable identity paths."""

    enabled: bool = True
    root: str = "prompts"
    user_profile: str = "identity/USER.md"
    working_memory: str = "identity/MEMORY.md"
    max_profile_chars: int = 6_000
    max_working_memory_chars: int = 8_000
    max_layer_chars: int = 20_000
    max_total_prompt_chars: int = 60_000


@dataclass
class ToolRoutingConfig:
    """Embedding-only selection of model-visible tools."""

    base_url: str = "http://192.168.0.10:8080/v1"
    model: str = "nvidia/Nemotron-3-Embed-1B-NVFP4"
    dimension: int = 2048
    top_k: int = 6
    max_active_tools: int = 12
    max_schema_bytes: int = 32_768
    min_similarity: float = 0.20
    timeout_seconds: float = 30.0


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
    extensions: ExtensionsConfig = field(default_factory=ExtensionsConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    tool_routing: ToolRoutingConfig = field(default_factory=ToolRoutingConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    websearch: WebSearchConfig = field(default_factory=WebSearchConfig)

    def to_dict(self) -> dict[str, Any]:
        return config_to_dict(self)


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
        extensions=ExtensionsConfig(
            active={
                "tool": [
                    "echo",
                    "filesystem",
                    "editor",
                    "shell",
                    "web.search",
                    "web.fetch",
                ],
                "channel_connector": [
                    "console.connector",
                    "telegram.connector",
                ],
                "model_provider": ["stub", "llm.local", "model.router"],
                "memory_provider": ["memory.none"],
                "policy_provider": ["security.policy"],
                "runtime_service": [
                    "gateway",
                    "prompts.runtime",
                    "memory.loop",
                    "context.manager",
                    "mcp.manager",
                    "skills.runtime",
                    "hooks.runtime",
                    "subagents.orchestrator",
                    "sandbox.executor",
                ],
                "storage_provider": ["state.file"],
                "observability": ["observability.jsonl"],
            },
            bindings={
                "planner": "stub",
                "primary_model": "model.router",
                "model_router": "model.router",
                "memory": "memory.none",
                "memory_loop": "memory.loop",
                "policy": "security.policy",
                "state": "state.file",
                "prompts": "prompts.runtime",
                "context": "context.manager",
                "mcp": "mcp.manager",
                "skills": "skills.runtime",
                "hooks": "hooks.runtime",
                "subagents": "subagents.orchestrator",
                "sandbox": "sandbox.executor",
                "observability": "observability.jsonl",
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
                "prompts.runtime": ExtensionSpec(
                    kind="runtime_service",
                    description="Layered Markdown prompt assembly",
                    path="../corax-prompt-runtime",
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
                "sandbox.executor": ExtensionSpec(
                    kind="runtime_service",
                    description="Fail-closed Seatbelt or Docker shell backend",
                    path="../corax-sandbox-executor",
                ),
                "observability.jsonl": ExtensionSpec(
                    kind="observability",
                    description="Privacy-first bounded JSONL execution traces",
                    path="../corax-observability",
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
                "web.fetch": ExtensionSpec(
                    kind="tool",
                    description="Guarded public-page fetch for grounded citations",
                    path="../corax-web-search-capability/web_fetch",
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
                "model.router": ExtensionSpec(
                    kind="model_provider",
                    description="Provider-agnostic model routing and fallback",
                    path="../corax-model-router",
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
            blocked_paths=["../corax-core", "../corax-sdk", "~/.ssh", ".env"],
        ),
        limits=LimitsConfig(
            max_parallel_tasks=4,
            max_plan_tasks=30,
            max_tasks_per_correlation=50,
            task_timeout_seconds=60,
            max_payload_mb=20,
        ),
        ui=UIConfig(theme="corax-hologram", mascot="corax", show_banner=True),
        llm=LLMConfig(
            base_url="http://192.168.0.10:8000/v1",
            model="google/gemma-4-12B-it",
            enable_image=False,
            enable_video=False,
        ),
        prompts=PromptsConfig(),
        tool_routing=ToolRoutingConfig(),
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
    return config


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
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
        "prompts": {
            "enabled": config.prompts.enabled,
            "root": config.prompts.root,
            "user_profile": config.prompts.user_profile,
            "working_memory": config.prompts.working_memory,
            "max_profile_chars": config.prompts.max_profile_chars,
            "max_working_memory_chars": config.prompts.max_working_memory_chars,
            "max_layer_chars": config.prompts.max_layer_chars,
            "max_total_prompt_chars": config.prompts.max_total_prompt_chars,
        },
        "tool_routing": {
            "base_url": config.tool_routing.base_url,
            "model": config.tool_routing.model,
            "dimension": config.tool_routing.dimension,
            "top_k": config.tool_routing.top_k,
            "max_active_tools": config.tool_routing.max_active_tools,
            "max_schema_bytes": config.tool_routing.max_schema_bytes,
            "min_similarity": config.tool_routing.min_similarity,
            "timeout_seconds": config.tool_routing.timeout_seconds,
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
    prompts = data.get("prompts", {}) or {}
    tool_routing = data.get("tool_routing", {}) or {}
    telegram = data.get("telegram", {}) or {}
    websearch = data.get("websearch", {}) or {}

    defaults = default_config()
    if "web.fetch" not in extensions.available:
        extensions.available["web.fetch"] = defaults.extensions.available[
            "web.fetch"
        ]
        extensions.active.setdefault("tool", []).append("web.fetch")
    if "prompts.runtime" not in extensions.available:
        extensions.available["prompts.runtime"] = defaults.extensions.available[
            "prompts.runtime"
        ]
        extensions.active.setdefault("runtime_service", []).append(
            "prompts.runtime"
        )
        extensions.bindings.setdefault("prompts", "prompts.runtime")
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
        prompts=PromptsConfig(
            enabled=bool(prompts.get("enabled", defaults.prompts.enabled)),
            root=str(prompts.get("root", defaults.prompts.root)),
            user_profile=str(
                prompts.get("user_profile", defaults.prompts.user_profile)
            ),
            working_memory=str(
                prompts.get("working_memory", defaults.prompts.working_memory)
            ),
            max_profile_chars=int(
                prompts.get(
                    "max_profile_chars",
                    defaults.prompts.max_profile_chars,
                )
            ),
            max_working_memory_chars=int(
                prompts.get(
                    "max_working_memory_chars",
                    defaults.prompts.max_working_memory_chars,
                )
            ),
            max_layer_chars=int(
                prompts.get("max_layer_chars", defaults.prompts.max_layer_chars)
            ),
            max_total_prompt_chars=int(
                prompts.get(
                    "max_total_prompt_chars",
                    defaults.prompts.max_total_prompt_chars,
                )
            ),
        ),
        tool_routing=ToolRoutingConfig(
            base_url=str(
                tool_routing.get(
                    "base_url",
                    defaults.tool_routing.base_url,
                )
            ),
            model=str(
                tool_routing.get("model", defaults.tool_routing.model)
            ),
            dimension=int(
                tool_routing.get(
                    "dimension",
                    defaults.tool_routing.dimension,
                )
            ),
            top_k=int(
                tool_routing.get("top_k", defaults.tool_routing.top_k)
            ),
            max_active_tools=int(
                tool_routing.get(
                    "max_active_tools",
                    defaults.tool_routing.max_active_tools,
                )
            ),
            max_schema_bytes=int(
                tool_routing.get(
                    "max_schema_bytes",
                    defaults.tool_routing.max_schema_bytes,
                )
            ),
            min_similarity=float(
                tool_routing.get(
                    "min_similarity",
                    defaults.tool_routing.min_similarity,
                )
            ),
            timeout_seconds=float(
                tool_routing.get(
                    "timeout_seconds",
                    defaults.tool_routing.timeout_seconds,
                )
            ),
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
        "model_router": "model_provider",
        "memory": "memory_provider",
        "memory_loop": "runtime_service",
        "policy": "policy_provider",
        "state": "storage_provider",
        "prompts": "runtime_service",
        "context": "runtime_service",
        "mcp": "runtime_service",
        "skills": "runtime_service",
        "hooks": "runtime_service",
        "subagents": "runtime_service",
        "sandbox": "runtime_service",
        "observability": "observability",
    }
    for role, extension_id in config.extensions.bindings.items():
        if not extension_id:
            continue
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

    for name in (
        "dimension",
        "top_k",
        "max_active_tools",
        "max_schema_bytes",
    ):
        value = getattr(config.tool_routing, name)
        if not isinstance(value, int) or value <= 0:
            errors.append(
                f"tool_routing.{name} must be a positive integer, got {value!r}"
            )
    if not 0 <= config.tool_routing.min_similarity <= 1:
        errors.append("tool_routing.min_similarity must be between 0 and 1")
    if config.tool_routing.timeout_seconds <= 0:
        errors.append("tool_routing.timeout_seconds must be positive")

    prompt_limits = {
        "max_profile_chars": (256, 64_000),
        "max_working_memory_chars": (256, 128_000),
        "max_layer_chars": (1_024, 256_000),
        "max_total_prompt_chars": (4_096, 1_000_000),
    }
    for name, (minimum, maximum) in prompt_limits.items():
        value = getattr(config.prompts, name)
        if not isinstance(value, int) or not minimum <= value <= maximum:
            errors.append(
                f"prompts.{name} must be an integer from {minimum} to "
                f"{maximum}, got {value!r}"
            )

    return errors
