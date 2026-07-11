"""Service layer — use-case orchestration (business-agnostic CRUD + search).

One class per use-case group, sitting between the executor (transport seam) and
the ``_engine`` mechanisms (duck / search / files):

  query.py   QueryService  — read: search rewrite → hybrid → engine query
  write.py   WriteService  — insert / delete: validate → files → DuckDB
  admin.py   AdminService  — rebuild: replay the file mirror into DuckDB

``build_services`` wires them from the engines; ``Services`` bundles the three
so the executor takes a single dependency.
"""
from __future__ import annotations

from dataclasses import dataclass

from .admin import AdminService
from .query import QueryService
from .write import WriteService


@dataclass(frozen=True)
class Services:
    query: QueryService
    write: WriteService
    admin: AdminService


def build_services(duck, search, files, bridge, schema) -> Services:
    return Services(
        query=QueryService(duck, search, schema),
        write=WriteService(duck, search, files, bridge, schema),
        admin=AdminService(duck, search, files, bridge, schema),
    )


__all__ = ["Services", "QueryService", "WriteService", "AdminService", "build_services"]
