"""POST /v1/insert — write rows → ``{"ticket", "state", …}``.

Synchronous: the row (vector included) is committed before the response; the
returned task is born ``done``. Re-inserting an existing primary key →
``QueryError`` (keys are write-once). See docs/api/insert.md.
"""
from __future__ import annotations

from ._route import Endpoint


async def handle(db, body: dict, params: dict) -> tuple[int, dict]:
    task = await db.services.write.insert(body.get("table"), body.get("rows") or [])
    return 200, task.to_wire()


ENDPOINT = Endpoint("POST", "/v1/insert", handle)
