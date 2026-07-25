"""Loaders for pluggable, out-of-tree modules.

Two seams, both with lazily-imported dependencies so the scaffold runs on a
pure-stdlib install:

* :class:`ExtensionLoader` — loads typed packages from ``extension.json``.
* :class:`CoreEngine` — wires the **execution kernel** from ``agent-core``.

Keeping both out of the runtime lets ``corax.runtime`` stay a thin lifecycle
owner.
"""

from __future__ import annotations

from .capabilities import CapabilityLoader, ExtensionLoader
from .core import ConfirmationRequired, CoreEngine, RunningCore

__all__ = [
    "CapabilityLoader",
    "ConfirmationRequired",
    "ExtensionLoader",
    "CoreEngine",
    "RunningCore",
]
