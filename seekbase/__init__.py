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
    NotFound,
    QueryError,
    ReadOnlyError,
    SchemaError,
    SeekbaseError,
    SeekbaseUnavailable,
)
from .client import Seekbase
from .struct import Hit, Request, Row, Ticket

__version__ = "0.0.1"

__all__ = [
    "Seekbase",
    "Embedder",
    "Row",
    "Hit",
    "Ticket",
    "Request",
    "SeekbaseError",
    "SeekbaseUnavailable",
    "SchemaError",
    "EmbedderInvalid",
    "ReadOnlyError",
    "QueryError",
    "NotFound",
    "__version__",
]
