"""POST /v1/insert — write rows → ``{"ticket", "state", …}``.

Synchronous: the row (vector included) is committed before the response; the
ticket is already ``done``. Re-inserting an existing primary key → ``QueryError``
(keys are write-once). See docs/api/insert.md.
"""
from __future__ import annotations

from .._engine.plan import Request
from ._route import Endpoint


async def handle(db, body: dict, params: dict) -> tuple[int, dict]:
    req = Request(op="insert", table=body.get("table"), rows=tuple(body.get("rows") or ()))
    return 200, await db._dispatch(req)


ENDPOINT = Endpoint("POST", "/v1/insert", handle)
