"""runtime — cross-cutting execution infrastructure.

Not a data store and not a use case: the single-writer bridge that serializes
DuckDB access, and the engine clock (ds / created_at formats).
"""
from __future__ import annotations

from .bridge import Bridge
from .clock import now, today
from .readpool import ReadPool

__all__ = ["Bridge", "ReadPool", "now", "today"]
