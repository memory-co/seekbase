"""TicketRegistry — write tickets.

Writes are synchronous, so a ticket is ``done`` the moment it is issued. The
registry exists only so ``status`` can tell a known ticket (→ done) from an
unknown one (→ NotFound / 404). Shared by the write / admin services (issue)
and the status endpoint (lookup).
"""
from __future__ import annotations

import uuid

from .._types import NotFound


class TicketRegistry:
    def __init__(self) -> None:
        self._tickets: dict[str, dict] = {}

    def issue(self, op: str, extra: dict | None = None) -> dict:
        tid = "wr_" + uuid.uuid4().hex[:16]
        meta = {"ticket": tid, "op": op, "error": None, **(extra or {})}
        self._tickets[tid] = meta
        return {**meta, "state": "done"}

    def status(self, ticket: str) -> dict:
        meta = self._tickets.get(ticket)
        if meta is None:
            raise NotFound(f"unknown ticket {ticket!r}")
        return {**meta, "state": "done"}
