"""Ticket — a write receipt.

Returned by insert / delete / rebuild and looked up by status. Writes are
synchronous, so a ticket is ``done`` the moment it exists. The dataclass is the
in-process currency; ``to_wire`` / ``from_wire`` convert at the HTTP boundary
(the JSON body uses ``"ticket"`` for the id).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Ticket:
    id: str
    op: str                       # insert / delete / rebuild
    state: str = "done"
    error: str | None = None
    matched: int | None = None    # delete: rows soft-deleted
    stats: dict | None = None     # rebuild: {tables, rows, tombstones}

    def to_wire(self) -> dict:
        d = {"ticket": self.id, "op": self.op, "state": self.state, "error": self.error}
        if self.matched is not None:
            d["matched"] = self.matched
        if self.stats is not None:
            d["stats"] = self.stats
        return d

    @classmethod
    def from_wire(cls, d: dict) -> "Ticket":
        return cls(
            id=d["ticket"], op=d.get("op", ""), state=d.get("state", "done"),
            error=d.get("error"), matched=d.get("matched"), stats=d.get("stats"),
        )
