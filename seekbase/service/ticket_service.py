"""TicketService — hands out and looks up write tickets.

Writes are synchronous, so a ticket is ``done`` the moment it is issued. The
registry exists only so ``status`` can tell a known ticket (→ the Ticket) from
an unknown one (→ NotFound / 404). Shared by the write / admin services (issue)
and the status endpoint (lookup). The Ticket itself lives in ``struct/``.
"""
from __future__ import annotations

import uuid

from .._types import NotFound
from ..struct import Ticket


class TicketService:
    def __init__(self) -> None:
        self._tickets: dict[str, Ticket] = {}

    def issue(self, op: str, *, matched: int | None = None, stats: dict | None = None) -> Ticket:
        t = Ticket(id="wr_" + uuid.uuid4().hex[:16], op=op, matched=matched, stats=stats)
        self._tickets[t.id] = t
        return t

    def status(self, ticket: str) -> Ticket:
        t = self._tickets.get(ticket)
        if t is None:
            raise NotFound(f"unknown ticket {ticket!r}")
        return t
