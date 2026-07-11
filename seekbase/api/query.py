"""POST /v1/query — read: run a SQL SELECT (+ ds time window) → ``{"rows": […]}``.

``search(col, 'text')`` and the ``ds_start`` / ``ds_end`` time machine live in
this one read endpoint. Read-only: non-SELECT → ``ReadOnlyError``.
See docs/api/query.md.
"""
from __future__ import annotations

from ._route import Endpoint


async def handle(db, body: dict, params: dict) -> tuple[int, dict]:
    return 200, await db.services.read.query(
        body.get("sql"), body.get("params") or [], body.get("ds_start"), body.get("ds_end"))


ENDPOINT = Endpoint("POST", "/v1/query", handle)
