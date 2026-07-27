"""Settings mutation layer.

A thin, typed API for reading and writing configuration values by dotted
key path, and for managing providers. The menu (and, later, any remote
admin surface) edits config exclusively through these functions so the
rules live in one place.

    get_setting(config, "agent.name")
    set_setting(config, "runtime.log_level", "DEBUG")
    toggle_provider(config, "planner", "stub", enabled=True)
    set_active_provider(config, "memory", "none")
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any

from .config import AgentConfig, ExtensionSpec

# Sections whose ``active`` is a single id vs. a list of ids.
_SCALAR_ACTIVE = {"planner", "memory"}
_LIST_ACTIVE = {"connectors": "active", "capabilities": "enabled"}
_PROVIDER_SECTIONS = {"planner", "memory", "connectors", "capabilities"}


class SettingError(KeyError):
    """Raised when a key path or provider id cannot be resolved."""


# --------------------------------------------------------------------------- #
# Generic get / set by dotted path
# --------------------------------------------------------------------------- #
def get_setting(config: AgentConfig, key_path: str) -> Any:
    """Read a value by dotted path, e.g. ``"runtime.autostart"``."""
    node: Any = config
    for part in key_path.split("."):
        node = _get_child(node, part, key_path)
    return node


def set_setting(config: AgentConfig, key_path: str, value: Any) -> AgentConfig:
    """Set a value by dotted path, coercing to the existing field's type.

    Returns the same (mutated) config for chaining.
    """
    parts = key_path.split(".")
    parent_path, leaf = parts[:-1], parts[-1]
    node: Any = config
    for part in parent_path:
        node = _get_child(node, part, key_path)

    current = _get_child(node, leaf, key_path)
    coerced = _coerce(value, current)

    if isinstance(node, dict):
        node[leaf] = coerced
    else:
        setattr(node, leaf, coerced)
    return config


def _get_child(node: Any, part: str, key_path: str) -> Any:
    if is_dataclass(node):
        valid = {f.name for f in fields(node)}
        if part not in valid:
            raise SettingError(f"unknown key segment '{part}' in '{key_path}'")
        return getattr(node, part)
    if isinstance(node, dict):
        if part not in node:
            raise SettingError(f"unknown key segment '{part}' in '{key_path}'")
        return node[part]
    raise SettingError(f"cannot descend into '{part}' for '{key_path}'")


def _coerce(value: Any, current: Any) -> Any:
    """Coerce ``value`` (often a string from the menu) to match ``current``."""
    if isinstance(current, bool):
        return _to_bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, list):
        if isinstance(value, list):
            return value
        # Comma-separated string -> list of trimmed items.
        return [item.strip() for item in str(value).split(",") if item.strip()]
    if current is None:
        return value
    return str(value)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


# --------------------------------------------------------------------------- #
# Provider management
# --------------------------------------------------------------------------- #
def _canonical_id(section: str, provider_id: str) -> str:
    if section == "memory" and provider_id == "none":
        return "memory.none"
    return provider_id


def _kind_for_section(section: str) -> str:
    return {
        "planner": "model_provider",
        "memory": "memory_provider",
        "connectors": "channel_connector",
        "capabilities": "tool",
    }[section]


def _extension_spec(
    config: AgentConfig,
    section: str,
    provider_id: str,
) -> tuple[str, ExtensionSpec]:
    if section not in _PROVIDER_SECTIONS:
        raise SettingError(f"unknown provider section '{section}'")
    extension_id = _canonical_id(section, provider_id)
    try:
        spec = config.extensions.available[extension_id]
    except KeyError:
        raise SettingError(f"{section} has no provider '{provider_id}'") from None
    if spec.kind != _kind_for_section(section):
        raise SettingError(
            f"{provider_id!r} is {spec.kind}, not {_kind_for_section(section)}"
        )
    return extension_id, spec


def toggle_provider(
    config: AgentConfig, section: str, provider_id: str, enabled: bool
) -> AgentConfig:
    """Enable or disable a provider within a section.

    Disabling a provider also removes it from any active/enabled list so
    the config stays internally consistent.
    """
    extension_id, spec = _extension_spec(config, section, provider_id)
    spec.enabled = bool(enabled)

    if not enabled:
        active = config.extensions.active.setdefault(spec.kind, [])
        if extension_id in active:
            active.remove(extension_id)
        for role, bound_id in config.extensions.bindings.items():
            if bound_id == extension_id:
                config.extensions.bindings[role] = ""
    return config


def set_active_provider(config: AgentConfig, section: str, provider_id: str) -> AgentConfig:
    """Make ``provider_id`` active within ``section``.

    For scalar sections (planner/memory) this sets ``active``. For list
    sections (connectors/capabilities) it adds the id to the active list.
    The provider must exist and be enabled.
    """
    extension_id, spec = _extension_spec(config, section, provider_id)
    if not spec.enabled:
        raise SettingError(f"{section} provider '{provider_id}' is disabled")

    if section in _SCALAR_ACTIVE:
        binding = "memory" if section == "memory" else "planner"
        config.extensions.bindings[binding] = extension_id
        active = config.extensions.active.setdefault(spec.kind, [])
        if extension_id not in active:
            active.append(extension_id)
    elif section in _LIST_ACTIVE:
        active = config.extensions.active.setdefault(spec.kind, [])
        if extension_id not in active:
            active.append(extension_id)
    else:
        raise SettingError(f"section '{section}' has no active selection")
    return config


def deactivate_provider(config: AgentConfig, section: str, provider_id: str) -> AgentConfig:
    """Remove ``provider_id`` from a list-based active selection."""
    if section not in _LIST_ACTIVE:
        raise SettingError(f"section '{section}' does not support deactivation")
    extension_id, spec = _extension_spec(config, section, provider_id)
    active = config.extensions.active.setdefault(spec.kind, [])
    if extension_id in active:
        active.remove(extension_id)
    return config
