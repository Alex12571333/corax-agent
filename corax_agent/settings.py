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

from .config import AgentConfig, ProviderSpec

# Sections whose ``active`` is a single id vs. a list of ids.
_SCALAR_ACTIVE = {"planner", "memory"}
_LIST_ACTIVE = {"connectors": "active", "capabilities": "enabled"}
# Where each section keeps its provider catalogue.
_PROVIDER_FIELD = {
    "planner": "providers",
    "memory": "providers",
    "connectors": "providers",
    "capabilities": "available",
}


class SettingError(KeyError):
    """Raised when a key path or provider id cannot be resolved."""


# --------------------------------------------------------------------------- #
# Generic get / set by dotted path
# --------------------------------------------------------------------------- #
def get_setting(config: AgentConfig, key_path: str) -> Any:
    """Read a value by dotted path, e.g. ``"security.allow_shell"``."""
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
def _provider_catalogue(config: AgentConfig, section: str) -> dict[str, ProviderSpec]:
    if section not in _PROVIDER_FIELD:
        raise SettingError(f"unknown provider section '{section}'")
    return getattr(getattr(config, section), _PROVIDER_FIELD[section])


def toggle_provider(
    config: AgentConfig, section: str, provider_id: str, enabled: bool
) -> AgentConfig:
    """Enable or disable a provider within a section.

    Disabling a provider also removes it from any active/enabled list so
    the config stays internally consistent.
    """
    catalogue = _provider_catalogue(config, section)
    if provider_id not in catalogue:
        raise SettingError(f"{section} has no provider '{provider_id}'")
    catalogue[provider_id].enabled = bool(enabled)

    if not enabled:
        if section in _LIST_ACTIVE:
            attr = _LIST_ACTIVE[section]
            active_list = getattr(getattr(config, section), attr)
            if provider_id in active_list:
                active_list.remove(provider_id)
        elif section in _SCALAR_ACTIVE:
            sect = getattr(config, section)
            if sect.active == provider_id:
                sect.active = "none" if section == "memory" else ""
    return config


def set_active_provider(config: AgentConfig, section: str, provider_id: str) -> AgentConfig:
    """Make ``provider_id`` active within ``section``.

    For scalar sections (planner/memory) this sets ``active``. For list
    sections (connectors/capabilities) it adds the id to the active list.
    The provider must exist and be enabled.
    """
    catalogue = _provider_catalogue(config, section)
    if provider_id not in catalogue:
        raise SettingError(f"{section} has no provider '{provider_id}'")
    if not catalogue[provider_id].enabled:
        raise SettingError(f"{section} provider '{provider_id}' is disabled")

    if section in _SCALAR_ACTIVE:
        getattr(config, section).active = provider_id
    elif section in _LIST_ACTIVE:
        attr = _LIST_ACTIVE[section]
        active_list = getattr(getattr(config, section), attr)
        if provider_id not in active_list:
            active_list.append(provider_id)
    else:
        raise SettingError(f"section '{section}' has no active selection")
    return config


def deactivate_provider(config: AgentConfig, section: str, provider_id: str) -> AgentConfig:
    """Remove ``provider_id`` from a list-based active selection."""
    if section not in _LIST_ACTIVE:
        raise SettingError(f"section '{section}' does not support deactivation")
    attr = _LIST_ACTIVE[section]
    active_list = getattr(getattr(config, section), attr)
    if provider_id in active_list:
        active_list.remove(provider_id)
    return config
