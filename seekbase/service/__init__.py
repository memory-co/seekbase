"""Service layer — use-case orchestration (business-agnostic CRUD + search).

One class per use-case group. Both entry points call these services directly —
the HTTP ``api/`` handlers, and the embedded port (via the thin LocalExecutor).
Each service owns its full response shape (rows / ticket), so callers just relay
it. Services sit above the ``_engine`` mechanisms (duck / search / files):

  query.py    QueryService  — read: search rewrite → hybrid → engine query
  write.py    WriteService  — insert / delete: validate → files → DuckDB
  admin.py    AdminService  — rebuild: replay the file mirror into DuckDB
  tickets.py  TicketRegistry — write tickets (issue on write, look up on status)

``build_services`` wires them from the engines; ``Services`` bundles them so a
caller takes a single dependency.
"""
from __future__ import annotations

from dataclasses import dataclass

from .admin import AdminService
from .query import QueryService
from .tickets import TicketRegistry
from .write import WriteService


@dataclass(frozen=True)
class Services:
    query: QueryService
    write: WriteService
    admin: AdminService
    tickets: TicketRegistry


def build_services(duck, search, files, bridge, schema) -> Services:
    tickets = TicketRegistry()
    return Services(
        query=QueryService(duck, search, schema),
        write=WriteService(duck, search, files, bridge, schema, tickets),
        admin=AdminService(duck, search, files, bridge, schema, tickets),
        tickets=tickets,
    )


__all__ = [
    "Services", "QueryService", "WriteService", "AdminService",
    "TicketRegistry", "build_services",
]
