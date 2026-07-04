"""Wire format shared by the HTTP client and server.

One JSON shape for a ``Request`` + ``as_of``, and a mapping between the error
hierarchy and HTTP status codes so an exception raised on the server surfaces
as the same exception type on the client.
"""
from __future__ import annotations

from typing import Any

from ._engine.plan import Predicate, Request
from ._types import (
    EmbedderInvalid,
    NotSupportedYet,
    QueryError,
    ReadOnlyError,
    SchemaError,
    SeekbaseError,
    SeekbaseUnavailable,
)


def serialize_request(req: Request, as_of: str | None) -> dict:
    return {
        "op": req.op,
        "table": req.table,
        "columns": list(req.columns),
        "predicates": [
            {"op": p.op, "column": p.column, "value": p.value} for p in req.predicates
        ],
        "orders": [[c, d] for c, d in req.orders],
        "limit": req.limit,
        "offset": req.offset,
        "rows": [dict(r) for r in req.rows],
        "statement": req.statement,
        "before": req.before,
        "as_of": as_of,
    }


def deserialize_request(payload: dict) -> tuple[Request, str | None]:
    req = Request(
        op=payload["op"],
        table=payload.get("table"),
        columns=tuple(payload.get("columns") or ()),
        predicates=tuple(
            Predicate(p["op"], p["column"], p.get("value"))
            for p in payload.get("predicates") or ()
        ),
        orders=tuple((c, bool(d)) for c, d in payload.get("orders") or ()),
        limit=payload.get("limit"),
        offset=payload.get("offset"),
        rows=tuple(payload.get("rows") or ()),
        statement=payload.get("statement"),
        before=payload.get("before"),
    )
    return req, payload.get("as_of")


# ─── error <-> HTTP status ─────────────────────────────────────────────

_ERROR_TYPES = {
    c.__name__: c
    for c in (
        SeekbaseError,
        SeekbaseUnavailable,
        SchemaError,
        EmbedderInvalid,
        ReadOnlyError,
        QueryError,
        NotSupportedYet,
    )
}


def status_for(exc: Exception) -> int:
    if isinstance(exc, NotSupportedYet):
        return 501
    if isinstance(exc, SeekbaseUnavailable):
        return 503
    if isinstance(exc, SeekbaseError):
        return 400  # ReadOnly / Query / Schema / Embedder / base
    return 500


def error_body(exc: Exception) -> dict:
    return {"type": type(exc).__name__, "message": str(exc)}


def exception_from(err: dict[str, Any]) -> Exception:
    cls = _ERROR_TYPES.get(err.get("type", ""), SeekbaseError)
    return cls(err.get("message", ""))
