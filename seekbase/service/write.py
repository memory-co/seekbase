"""WriteService — the write use cases (insert / delete).

Orchestrates the three subsystems in order: validate → embed (search) →
**files first** (canonical mirror) → DuckDB (row commit / soft-delete). Files
lead so the mirror can recalibrate the derived DuckDB on ``rebuild``. Writes are
synchronous; primary keys are write-once (re-insert → error).
"""
from __future__ import annotations

from .._engine.clock import now, today
from .._types import QueryError
from ..schema import CREATED_AT, DELETED_AT, DS


class WriteService:
    def __init__(self, duck, search, files, bridge, schema) -> None:
        self._duck = duck
        self._search = search
        self._files = files
        self._bridge = bridge
        self._schema = schema

    async def insert(self, table: str, rows: list[dict]) -> None:
        spec = self._schema.table(table)
        records: list[dict] = []
        for row in rows:
            unknown = set(row) - set(spec.column_names)
            if unknown:
                raise QueryError(f"{table}: unknown column(s) {sorted(unknown)}")
            records.append({c: row.get(c) for c in spec.column_names})

        pk = spec.primary_key
        keys = [str(r[pk]) for r in records]
        if len(set(keys)) != len(keys):
            raise QueryError(f"{table}: duplicate primary key within the insert batch")
        existing = await self._duck.existing_keys(table, keys)
        if existing:
            raise QueryError(
                f"{table}: primary key already exists: {existing[0]!r} "
                f"(seekbase is insert-only; a key is written once)")

        vecs, toks = ({}, {})
        if self._search is not None:
            vecs, toks = await self._search.embed_records(spec, records)

        ds, ts = today(), now()
        mrecs = [{**{c: rec[c] for c in spec.column_names}, DS: ds, CREATED_AT: ts}
                 for rec in records]
        await self._bridge.run(lambda: [self._files.append(ds, table, m) for m in mrecs])
        await self._duck.commit_rows(spec, records, vecs, toks, ds, ts)

    async def delete(self, table: str, where: str, params) -> int:
        keys = await self._duck.match_live(table, where, list(params))
        ds, ts = today(), now()
        await self._bridge.run(
            lambda: [self._files.append(ds, table, {"_deleted": k, DELETED_AT: ts}) for k in keys])
        await self._duck.soft_delete(table, keys, ds, ts)
        return len(keys)
