"""Engine clock — UTC timestamps and the ds partition key.

Kept in one place so the write path (ds / created_at stamping) and any engine
code agree on the format: ``ds`` is ``YYYYMMDD`` UTC, ``now`` is ISO-8601 UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")
