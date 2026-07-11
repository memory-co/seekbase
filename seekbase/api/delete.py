"""POST /v1/delete — soft-delete rows matching ``where`` → ``{"ticket", "state", "matched"}``.

Delete is a tombstone: it stamps ``deleted_ds`` / ``deleted_at`` on the row
(history is permanent, no physical delete). See docs/api/delete.md.
"""
from __future__ import annotations

from ._route import Endpoint


async def handle(db, body: dict, params: dict) -> tuple[int, dict]:
    return 200, await db.services.write.delete(
        body.get("table"), body.get("where"), body.get("params") or [])


ENDPOINT = Endpoint("POST", "/v1/delete", handle)
