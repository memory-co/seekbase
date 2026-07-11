"""Service layer — domain services + use-case services (business-agnostic).

Everything is a service here; ``_engine`` is gone. Two kinds:

  domain services (own a subdomain end-to-end)
    store.py    StoreService   — DuckDB structured: DDL, validate, commit, query
    search.py   SearchService  — vss + fts: embed, tokenize, index, hybrid
    files.py    FileService    — canonical file mirror: record/tombstone shapes

  use-case services (thin orchestrators: order + policy only)
    query.py    QueryService   — read: rewrite → hybrid → store query
    write.py    WriteService   — insert / delete: validate → files → store
    admin.py    AdminService   — rebuild: replay the file mirror into the store
    tickets.py  TicketRegistry — write tickets (issue on write, look up on status)
    dispatch.py LocalExecutor  — map a Request's op to a service (the local seam)

``build_services`` wires the use-case services onto the domain ones; ``Services``
bundles them. Both entry points (HTTP ``api/`` handlers, embedded client via
LocalExecutor) call the same services.
"""
from __future__ import annotations

from dataclasses import dataclass

from .admin import AdminService
from .dispatch import LocalExecutor
from .files import FileService
from .query import QueryService
from .search import SearchService
from .store import StoreService
from .tickets import TicketRegistry
from .write import WriteService


@dataclass(frozen=True)
class Services:
    query: QueryService
    write: WriteService
    admin: AdminService
    tickets: TicketRegistry


def build_services(store, search, files, schema) -> Services:
    tickets = TicketRegistry()
    return Services(
        query=QueryService(store, search, schema),
        write=WriteService(store, search, files, schema, tickets),
        admin=AdminService(store, search, files, schema, tickets),
        tickets=tickets,
    )


__all__ = [
    "Services", "build_services",
    "StoreService", "SearchService", "FileService",
    "QueryService", "WriteService", "AdminService",
    "TicketRegistry", "LocalExecutor",
]
