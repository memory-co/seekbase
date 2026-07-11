"""GET /v1/writes/{ticket} — poll a write's status → ``{"ticket", "state", …}``.

Writes are synchronous, so a known ticket is always ``done``; an unknown ticket
→ ``NotFound`` (404). See docs/api/insert.md.
"""
from __future__ import annotations

from .._engine.plan import Request
from ._route import Endpoint


async def handle(db, body: dict, params: dict) -> tuple[int, dict]:
    req = Request(op="status", ticket=params["ticket"])
    return 200, await db._dispatch(req)


ENDPOINT = Endpoint("GET", "/v1/writes/{ticket}", handle)
