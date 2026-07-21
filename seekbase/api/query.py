"""POST /v1/query — read: run a pipeline (SQL by default, + ds time window)
→ ``{"rows": […]}``.

A query is ``stage | stage``: a segment whose leading token hits the operator
registry (``search``/``scan``/``grep``) is that operator, anything else is one
DuckDB SQL statement; a pure SQL query has zero pipes. The ``ds_start`` /
``ds_end`` time machine applies to the whole pipeline (search candidates
included). Read-only: non-SELECT → ``ReadOnlyError``. See docs/api/query.md.
"""
from __future__ import annotations

from ._route import Endpoint


async def handle(db, body: dict, params: dict) -> tuple[int, dict]:
    return 200, await db.services.read.query(
        body.get("sql"), body.get("params") or [], body.get("ds_start"), body.get("ds_end"))


ENDPOINT = Endpoint("POST", "/v1/query", handle)
