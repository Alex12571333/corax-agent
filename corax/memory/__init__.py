"""Memory role.

Memory backends persist and recall context. The built-in
:class:`~corax.memory.none.NullMemory` stores nothing; SQLite / vector
stores plug in under the same role later.
"""

from __future__ import annotations

from .none import NullMemory

__all__ = ["NullMemory"]
