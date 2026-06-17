"""Adapter for the standalone Corax tool-discovery plugin.

The plugin is deliberately manifest-only: it scans ``capability.json`` files
from configured capability packages and returns ids for the small active tool
set the model should see on a given user turn.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from .config import AgentConfig

_INTERNAL_IDS = ("gateway", "llm.local", "telegram.connector")


class RuntimeToolSelector:
    """Select active capability ids for a single user request."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        root_path: str | Path,
        top_k: int = 8,
        log: logging.Logger | None = None,
    ) -> None:
        self.top_k = top_k
        self.log = log or logging.getLogger("corax.tool_discovery")
        self._selector = None
        self._options_cls = None

        try:
            from tool_discovery import SelectionOptions, ToolCatalog, ToolSelector
        except ImportError:
            self.log.debug("corax-plugin-tool-discovery is not installed")
            return

        roots = list(_manifest_roots(config, root_path=root_path))
        if not roots:
            return
        catalog = ToolCatalog.from_roots(roots)
        if len(catalog) == 0:
            return
        self._selector = ToolSelector(catalog)
        self._options_cls = SelectionOptions
        self.log.info("tool discovery ready: %s manifest(s)", len(catalog))

    @property
    def available(self) -> bool:
        return self._selector is not None and self._options_cls is not None

    def select(self, user_text: str, _available_specs: list[dict]) -> list[str]:
        """Return capability ids selected for ``user_text``.

        ``_available_specs`` is accepted so the gateway can use the same callable
        shape for future selector implementations that inspect runtime health.
        """
        if not self.available:
            return []
        options = self._options_cls(
            top_k=self.top_k,
            exclude_ids=_INTERNAL_IDS,
            capability_type="tool",
            max_permission="dangerous",
            max_risk="critical",
        )
        return [manifest.id for manifest in self._selector.select(user_text, options)]


def _manifest_roots(config: AgentConfig, *, root_path: str | Path) -> Iterable[Path]:
    root = Path(root_path)
    for cap_id in config.capabilities.enabled:
        if cap_id in _INTERNAL_IDS:
            continue
        spec = config.capabilities.available.get(cap_id)
        if spec is None or not spec.enabled or not spec.path:
            continue
        path = Path(spec.path).expanduser()
        if not path.is_absolute():
            path = root / path
        yield path.resolve()
