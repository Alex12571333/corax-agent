"""Filesystem path resolution and safety helpers.

Paths in the config are relative to the directory that holds the config
file (the "project root"). This module resolves them, creates them and
exposes a guard that keeps the agent from touching blocked locations
(notably ``corax-core`` / ``corax-sdk``, which must never be modified).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import AgentConfig

# Candidate config filenames, in the order we look for them.
CONFIG_CANDIDATES = ("agent.yaml", "agent.yml", "agent.json", "config.json")


@dataclass
class AgentPaths:
    """Resolved, absolute paths used by the runtime."""

    root: Path
    config: Path
    workspace: Path
    data: Path
    logs: Path

    @classmethod
    def from_config(cls, config: AgentConfig, config_path: Path) -> "AgentPaths":
        config_path = Path(config_path).resolve()
        root = config_path.parent
        return cls(
            root=root,
            config=config_path,
            workspace=resolve_path(root, config.runtime.workspace_path),
            data=resolve_path(root, config.runtime.data_path),
            logs=resolve_path(root, config.runtime.logs_path),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "config": str(self.config),
            "workspace": str(self.workspace),
            "data": str(self.data),
            "logs": str(self.logs),
        }


def resolve_path(base: Path, value: str) -> Path:
    """Resolve ``value`` against ``base``, expanding ``~`` and env vars."""
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def ensure_paths(config: AgentConfig, config_path: Path) -> AgentPaths:
    """Create workspace / data / logs directories and return resolved paths."""
    paths = AgentPaths.from_config(config, config_path)
    for directory in (paths.workspace, paths.data, paths.logs):
        directory.mkdir(parents=True, exist_ok=True)
    return paths


def default_config_path(base: Path | None = None) -> Path:
    """Pick a config path: an existing candidate, else a sensible default."""
    base = Path(base) if base else Path.cwd()
    for name in CONFIG_CANDIDATES:
        candidate = base / name
        if candidate.exists():
            return candidate
    # Nothing on disk yet — prefer YAML when PyYAML is available, else JSON.
    from . import _yaml

    return base / ("agent.yaml" if _yaml.HAS_PYYAML else "agent.json")


def is_blocked_path(config: AgentConfig, target: Path, base: Path) -> bool:
    """Return True if ``target`` falls under any configured blocked path.

    Used by the security guard so future file/shell capabilities cannot
    write into ``corax-core``, ``corax-sdk``, ``~/.ssh`` or ``.env``.
    """
    target_resolved = resolve_path(base, str(target))
    for blocked in config.security.blocked_paths:
        blocked_resolved = resolve_path(base, blocked)
        if target_resolved == blocked_resolved:
            return True
        if _is_relative_to(target_resolved, blocked_resolved):
            return True
    return False


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False
