"""GET /v1/health — liveness → ``{"ready": bool}``.

No auth-sensitive data; a plain readiness probe for the host / load balancer.
"""
from __future__ import annotations

from ._route import Endpoint


async def handle(db, body: dict, params: dict) -> tuple[int, dict]:
    return 200, {"ready": db.ready}


ENDPOINT = Endpoint("GET", "/v1/health", handle)
