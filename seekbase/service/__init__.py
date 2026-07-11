"""Service layer — every file here is a ``*_service.py``. Two kinds:

  domain services (own a subdomain end-to-end)
    store_service.py    StoreService   — DuckDB structured: DDL, validate, commit, query
    search_service.py   SearchService  — vss + fts: embed, tokenize, index, hybrid
    file_service.py     FileService    — canonical file mirror: record/tombstone shapes

  use-case services (thin orchestrators: order + policy only)
    read_service.py     ReadService    — read: rewrite → hybrid → store query
    write_service.py    WriteService   — insert / delete: validate → files → store
    admin_service.py    AdminService   — rebuild: replay the file mirror into the store
    ticket_service.py   TicketService  — write tickets (issue on write, look up on status)

``build_services`` wires the use-case services onto the domain ones; ``Services``
bundles them. The local execution seam (LocalExecutor) lives in ``client.py``;
the HTTP one in ``api/remote.py``.
"""
from __future__ import annotations

from dataclasses import dataclass

from .admin_service import AdminService
from .file_service import FileService
from .read_service import ReadService
from .search_service import SearchService
from .store_service import StoreService
from .ticket_service import TicketService
from .write_service import WriteService


@dataclass(frozen=True)
class Services:
    read: ReadService
    write: WriteService
    admin: AdminService
    tickets: TicketService


def build_services(store, search, files, schema) -> Services:
    tickets = TicketService()
    return Services(
        read=ReadService(store, search, schema),
        write=WriteService(store, search, files, schema, tickets),
        admin=AdminService(store, search, files, schema, tickets),
        tickets=tickets,
    )


__all__ = [
    "Services", "build_services",
    "StoreService", "SearchService", "FileService",
    "ReadService", "WriteService", "AdminService", "TicketService",
]
