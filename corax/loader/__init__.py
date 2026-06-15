"""Loaders for pluggable, out-of-tree modules.

Two seams, both with lazily-imported dependencies so the scaffold runs on a
pure-stdlib install:

* :class:`CapabilityLoader` — loads **capability packages** built on
  ``agent-sdk`` from their ``capability.json`` manifests.
* :class:`CoreEngine` — wires the **execution kernel** from ``agent-core``.

Keeping both out of the runtime lets ``corax.runtime`` stay a thin lifecycle
owner.
"""

from __future__ import annotations

from .capabilities import CapabilityLoader
from .core import CoreEngine, RunningCore

__all__ = ["CapabilityLoader", "CoreEngine", "RunningCore"]
