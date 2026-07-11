"""Row / Hit — the shape of query results.

A row is a plain dict (column name → value); a Hit is a Row plus a
``_score_<col>`` float from ``search()``. Kept as aliases (not classes) so
results serialize to JSON as-is.
"""
from __future__ import annotations

from typing import Any

Row = dict[str, Any]
Hit = dict[str, Any]
