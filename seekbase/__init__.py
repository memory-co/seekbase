"""seekbase — a supabase-style embedded data port with semantic search as a
first-class operator.

Public surface: the ``Seekbase`` port, value types, the ``Embedder`` injection
protocol, and the error hierarchy. The engine (DuckDB — structured + vss + fts —
and the file mirror) lives behind the port and is not exported.
"""
from __future__ import annotations

from ._types import (
    Embedder,
    EmbedderInvalid,
    Hit,
    NotFound,
    QueryError,
    ReadOnlyError,
    Row,
    SchemaError,
    SeekbaseError,
    SeekbaseUnavailable,
)
from .port import Seekbase

__version__ = "0.0.1"

__all__ = [
    "Seekbase",
    "Embedder",
    "Row",
    "Hit",
    "SeekbaseError",
    "SeekbaseUnavailable",
    "SchemaError",
    "EmbedderInvalid",
    "ReadOnlyError",
    "QueryError",
    "NotFound",
    "__version__",
]
