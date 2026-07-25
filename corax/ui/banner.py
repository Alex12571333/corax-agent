"""Compatibility export for the shared Corax wordmark."""

from __future__ import annotations

from corax_ui import LOGO

BANNER = "\n" + "\n".join(LOGO) + "\n"

__all__ = ["BANNER"]
