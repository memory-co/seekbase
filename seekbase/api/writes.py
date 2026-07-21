"""GET /v1/writes/{ticket} — compatibility alias for ``/v1/tasks/{id}``.

A ticket is a born-done task (docs/works/task.md §2); this route delegates to
the shared task log. Unknown id → ``NotFound`` (404). See docs/api/insert.md.
"""
from __future__ import annotations

from ._route import Endpoint


async def handle(db, body: dict, params: dict) -> tuple[int, dict]:
    return 200, (await db.services.write.status(params["ticket"])).to_wire()


ENDPOINT = Endpoint("GET", "/v1/writes/{ticket}", handle)
