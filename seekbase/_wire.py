"""Wire helpers shared by the HTTP client and server.

Per-endpoint request bodies are tiny and built inline (executor / server); this
module owns the error<->HTTP mapping so an exception raised on the server
surfaces as the same exception type on the client.
"""
from __future__ import annotations

from typing import Any

from ._types import (
    EmbedderInvalid,
    NotFound,
    QueryError,
    ReadOnlyError,
    SchemaError,
    SeekbaseError,
    SeekbaseUnavailable,
)

_ERROR_TYPES = {
    c.__name__: c
    for c in (
        SeekbaseError,
        SeekbaseUnavailable,
        SchemaError,
        EmbedderInvalid,
        ReadOnlyError,
        QueryError,
        NotFound,
    )
}


def status_for(exc: Exception) -> int:
    if isinstance(exc, SeekbaseUnavailable):
        return 503
    if isinstance(exc, NotFound):
        return 404
    if isinstance(exc, SeekbaseError):
        return 400  # ReadOnly / Query / Schema / Embedder / base
    return 500


def error_body(exc: Exception) -> dict:
    return {"type": type(exc).__name__, "message": str(exc)}


def exception_from(err: dict[str, Any]) -> Exception:
    cls = _ERROR_TYPES.get(err.get("type", ""), SeekbaseError)
    return cls(err.get("message", ""))
