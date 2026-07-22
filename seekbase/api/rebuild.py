"""POST /v1/rebuild — admin: replay the file mirror into DuckDB → ``{"ticket", "state", "stats"}``.

Files are canonical; this rebuilds the derived DuckDB (rows + search indexes)
from them — as a background task: responds immediately with a pending task,
poll it to done via /v1/tasks/{id}. See docs/api/admin.md.
"""
from __future__ import annotations

from ._route import Endpoint


async def handle(db, body: dict, params: dict) -> tuple[int, dict]:
    task = await db.services.admin.rebuild()
    return 200, task.to_wire()


ENDPOINT = Endpoint("POST", "/v1/rebuild", handle)
