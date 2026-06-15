"""Capability role.

Capabilities are the tools the agent can invoke. The built-in
:class:`~corax.capabilities.echo.EchoCapability` ships in-tree; richer
capabilities (filesystem, editor, shell, …) are standalone SDK packages
loaded at runtime by :mod:`corax.loader.capabilities`.
"""

from __future__ import annotations

from .echo import EchoCapability

__all__ = ["EchoCapability"]
