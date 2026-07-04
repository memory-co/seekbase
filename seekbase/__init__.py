"""seekbase — a supabase-style embedded data port with semantic search as a
first-class operator.

Public surface: the ``Seekbase`` port, the ``QueryBuilder`` chain, value types,
the ``Embedder`` injection protocol, and the error hierarchy. Engines
(DuckDB / LanceDB / files / outbox) live behind the port and are not exported.
"""
from __future__ import annotations

from ._types import (
    Embedder,
    EmbedderInvalid,
    Hit,
    NotSupportedYet,
    QueryError,
    ReadOnlyError,
    Row,
    SchemaError,
    SeekbaseError,
    SeekbaseUnavailable,
)
from .port import QueryBuilder, Seekbase

__version__ = "0.0.1"

__all__ = [
    "Seekbase",
    "QueryBuilder",
    "Embedder",
    "Row",
    "Hit",
    "SeekbaseError",
    "SeekbaseUnavailable",
    "SchemaError",
    "EmbedderInvalid",
    "ReadOnlyError",
    "QueryError",
    "NotSupportedYet",
    "__version__",
]
