"""POST /v1/delete — soft-delete rows matching ``where`` → ``{"ticket", "state", "matched"}``.

Delete is a tombstone: it stamps ``deleted_ds`` / ``deleted_at`` on the row
(history is permanent, no physical delete). See docs/api/delete.md.
"""
from __future__ import annotations

from .._engine.plan import Request
from ._route import Endpoint


async def handle(db, body: dict, params: dict) -> tuple[int, dict]:
    req = Request(
        op="delete",
        table=body.get("table"),
        where=body.get("where"),
        params=tuple(body.get("params") or ()),
    )
    return 200, await db._dispatch(req)


ENDPOINT = Endpoint("POST", "/v1/delete", handle)
