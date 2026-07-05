"""Transport-neutral request type.

``Request`` is the single unit that flows through an executor — embedded
(straight to DuckDB) or over HTTP (serialized to the server). One shape both
sides agree on. Operations mirror the API docs:

  read:   query
  write:  insert / delete            (async: return a ticket)
  poll:   status                     (GET /v1/writes/{ticket})
  admin:  rebuild / vacuum
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Request:
    op: str
    # query
    sql: str | None = None
    params: tuple = ()
    ds_start: str | None = None
    ds_end: str | None = None
    # writes
    table: str | None = None
    rows: tuple[dict, ...] = ()        # insert
    where: str | None = None          # delete
    # poll / admin
    ticket: str | None = None         # status
    before: str | None = None         # vacuum
    _extra: dict = field(default_factory=dict)


# result carriers (plain dicts on the wire; these are just for clarity)
Row = dict[str, Any]
