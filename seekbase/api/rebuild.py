"""POST /v1/rebuild — admin: replay the file mirror into DuckDB → ``{"ticket", "state", "stats"}``.

Files are canonical; this rebuilds the derived DuckDB (rows + vss/fts indexes)
from them. See docs/api/admin.md.
"""
from __future__ import annotations

from ._route import Endpoint


async def handle(db, body: dict, params: dict) -> tuple[int, dict]:
    return 200, await db.services.admin.rebuild()


ENDPOINT = Endpoint("POST", "/v1/rebuild", handle)
