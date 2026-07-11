"""AdminService — administrative use cases (rebuild).

``rebuild`` replays the canonical file mirror into the derived DuckDB: read
every table's put/delete events, re-embed the puts, clear + reload the rows,
re-apply the soft-deletes, and refresh each table's FTS index once.
"""
from __future__ import annotations

from ..runtime import now
from ..struct import CREATED_AT, DELETED_AT, DS


class AdminService:
    def __init__(self, store, search, files, schema, tickets) -> None:
        self._store = store
        self._search = search
        self._files = files
        self._schema = schema
        self._tickets = tickets

    async def rebuild(self) -> dict:
        result = {"tables": 0, "rows": 0, "tombstones": 0}

        # 1) read events (filesystem) + embed the puts, per table
        replay: dict[str, dict] = {}
        for spec in self._schema.tables:
            puts, dels = [], []
            for ds, rec in self._files.iter_events(spec.name):
                if "_deleted" in rec:
                    dels.append((str(rec["_deleted"]), ds, rec.get(DELETED_AT)))
                else:
                    puts.append(rec)
            recs = [{c: p.get(c) for c in spec.column_names} for p in puts]
            vecs, toks = ({}, {})
            if self._search is not None:
                vecs, toks = await self._search.embed_records(spec, recs)
            replay[spec.name] = {
                "recs": recs, "vecs": vecs, "toks": toks, "dels": dels,
                "ds": [p.get(DS) for p in puts],
                "ca": [p.get(CREATED_AT) for p in puts],
            }

        # 2) clear, reload rows, re-apply deletes, refresh FTS once per table
        for spec in self._schema.tables:
            await self._store.clear(spec.name)
        for spec in self._schema.tables:
            result["tables"] += 1
            r = replay[spec.name]
            for i, rec in enumerate(r["recs"]):
                await self._store.commit_rows(
                    spec, [rec],
                    {c: [r["vecs"][c][i]] for c in r["vecs"]},
                    {c: [r["toks"][c][i]] for c in r["toks"]},
                    r["ds"][i], r["ca"][i] or now(), rebuild_fts=False)
                result["rows"] += 1
            for pk_val, dds, dat in r["dels"]:
                await self._store.soft_delete(spec.name, [pk_val], dds, dat)
                result["tombstones"] += 1
            await self._store.rebuild_fts(spec.name)
        return self._tickets.issue("rebuild", stats=result)
