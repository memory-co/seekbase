"""GET /v1/writes/{ticket} — poll a write's status → ``{"ticket", "state", …}``.

Writes are synchronous, so a known ticket is always ``done``; an unknown ticket
→ ``NotFound`` (404). See docs/api/insert.md.
"""
from __future__ import annotations

from ._route import Endpoint


async def handle(db, body: dict, params: dict) -> tuple[int, dict]:
    return 200, db.services.tickets.status(params["ticket"]).to_wire()


ENDPOINT = Endpoint("GET", "/v1/writes/{ticket}", handle)
