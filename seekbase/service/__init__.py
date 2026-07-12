"""Service layer — every file here is a ``*_service.py``. Two kinds:

  domain services (own a subdomain end-to-end)
    store_service.py      StoreService     — the DuckDB engine: structured + vss + fts (one connection)
    embedding_service.py  EmbeddingService — text → vectors + tokens (wraps the injected Embedder + jieba)
    file_service.py       FileService      — canonical file mirror: record/tombstone shapes

  use-case services (thin orchestrators: order + policy only)
    read_service.py     ReadService    — read: rewrite → embed → store.hybrid → store query
    write_service.py    WriteService   — insert / delete via one worker; owns the ticket log
    admin_service.py    AdminService   — rebuild: replay the file mirror into the store

The **ticket** concept lives inside WriteService (issue / status / append are its
methods — no standalone TicketService); admin issues its rebuild ticket via it.
``build_services`` wires the use-case services onto the domain ones; ``Services``
bundles them. The local execution seam (LocalExecutor) lives in ``client.py``.
"""
from __future__ import annotations

from dataclasses import dataclass

from .admin_service import AdminService
from .embedding_service import EmbeddingService
from .file_service import FileService
from .read_service import ReadService
from .store_service import StoreService
from .write_service import WriteService


@dataclass(frozen=True)
class Services:
    read: ReadService
    write: WriteService
    admin: AdminService


def build_services(store, embedding, files, schema, bridge, tickets_dir) -> Services:
    write = WriteService(store, embedding, files, schema, bridge, tickets_dir)
    return Services(
        read=ReadService(store, embedding, schema),
        write=write,
        admin=AdminService(store, embedding, files, schema, write),   # rebuild → write.issue
    )


__all__ = [
    "Services", "build_services",
    "StoreService", "EmbeddingService", "FileService",
    "ReadService", "WriteService", "AdminService",
]
