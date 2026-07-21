"""Task — the unified operation handle (docs/works/task.md).

Everything that *completes* is a task: a write (synchronous, born ``done`` —
the old ticket, semantics unchanged), a rebuild (a real pending→done
background job), a slow query (explicit ``as_task`` / HTTP timeout
escalation). One id scheme (``tk_<ds>_<hex>`` — ds-embedded, self-locating),
one status/wait surface, one ds-partitioned JSONL log.

The record stores the *query text* only; result rows live in a side file
(``tasks/results/<id>.jsonl``) referenced implicitly by id. ``to_wire`` /
``from_wire`` convert at the HTTP boundary (the JSON body uses ``"task"``
for the id; ``"ticket"`` is accepted on the way in for compatibility).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    id: str
    op: str                       # insert / delete / rebuild / query
    state: str = "done"           # pending / running / done / failed / cancelled
    error: str | None = None
    matched: int | None = None    # delete: rows soft-deleted
    stats: dict | None = None     # rebuild: {tables, rows, tombstones}
    query: str | None = None      # op=query: the SPL text (results go to a file)
    rows: int | None = None       # op=query, done: result row count
    submitted_at: str | None = None
    finished_at: str | None = None

    def to_wire(self) -> dict:
        d = {"task": self.id, "op": self.op, "state": self.state, "error": self.error}
        for k in ("matched", "stats", "query", "rows", "submitted_at", "finished_at"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d

    @classmethod
    def from_wire(cls, d: dict) -> "Task":
        return cls(
            id=d.get("task") or d["ticket"], op=d.get("op", ""),
            state=d.get("state", "done"), error=d.get("error"),
            matched=d.get("matched"), stats=d.get("stats"),
            query=d.get("query"), rows=d.get("rows"),
            submitted_at=d.get("submitted_at"), finished_at=d.get("finished_at"),
        )


Ticket = Task     # the old name: a ticket is a born-done task (task.md §2)
