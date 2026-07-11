"""WriteService — the write use cases (insert / delete).

A thin orchestrator: it owns only the cross-subdomain **order + atomicity**
(files first, then DuckDB), and delegates each subdomain's own logic to its
service — StoreService (validation, dup-pk, row commit), SearchService (embed +
tokenize), FileService (the on-disk record/tombstone shapes). Writes are
synchronous; primary keys are write-once (re-insert → error).
"""
from __future__ import annotations

from .._types import QueryError
from ..runtime import now, today


class WriteService:
    def __init__(self, store, search, files, schema, tickets) -> None:
        self._store = store
        self._search = search
        self._files = files
        self._schema = schema
        self._tickets = tickets

    async def insert(self, table: str, rows: list[dict]):
        records = await self._store.validate(table, rows)     # cols + write-once pk
        spec = self._schema.table(table)
        vecs, toks = ({}, {})
        if self._search is not None:
            vecs, toks = await self._search.embed_records(spec, records)
        ds, ts = today(), now()
        await self._files.write_puts(spec, records, ds, ts)   # canonical, files first
        await self._store.commit_rows(spec, records, vecs, toks, ds, ts)  # rows + FTS (atomic)
        return self._tickets.issue("insert")                  # -> struct.Ticket

    async def delete(self, table: str, where: str | None, params):
        if not where:
            raise QueryError("delete requires a where clause")
        keys = await self._store.match_live(table, where, list(params))
        ds, ts = today(), now()
        await self._files.write_deletes(table, keys, ds, ts)  # tombstone lines, files first
        await self._store.soft_delete(table, keys, ds, ts)
        return self._tickets.issue("delete", matched=len(keys))
