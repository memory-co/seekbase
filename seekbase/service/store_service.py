"""StoreService — the structured-storage subdomain (DuckDB).

Owns the DuckDB side: one physical table per business table, ``_sb_<table>``,
with business columns + metadata (ds / created_at / deleted_ds / deleted_at) +,
per searchable column, ``_vec_<col>`` (HNSW) and ``_tok_<col>`` (BM25). The
primary key is write-once. It owns structured validation (unknown columns,
dup-pk), the row primitives (``commit_rows`` / ``match_live`` / ``soft_delete``
/ ``clear``) and read (``run_query`` — read-only guard + visibility view +
search-score joins). It shares its DuckDB connection with SearchService (single
engine): ``commit_rows`` refreshes the touched FTS index in the same block.

The use-case services (write / query / admin) sequence StoreService with
FileService and SearchService.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import duckdb

from .._types import QueryError, ReadOnlyError
from ..runtime import Bridge
from ..struct import CREATED_AT, DELETED_AT, DELETED_DS, DS, Schema, TableSpec
from .search_service import SearchService, tokcol, veccol

__all__ = ["StoreService"]

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DS_RE = re.compile(r"^\d{8}$")


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise QueryError(f"illegal identifier {name!r}")
    return f'"{name}"'


def _phys(name: str) -> str:
    return _ident(f"_sb_{name}")


def _check_ds(label: str, value: str | None) -> None:
    if value is not None and not _DS_RE.match(value):
        raise QueryError(f"{label} must be YYYYMMDD, got {value!r}")


def _one_statement(sql: str, label: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        raise QueryError(f"{label} must be a single statement")
    return s


class StoreService:
    def __init__(self, bridge: Bridge, conn, schema: Schema, dim: int | None) -> None:
        self._bridge = bridge
        self._conn = conn
        self._schema = schema
        self._dim = dim               # embedding dim, or None if nothing searchable
        self._search: SearchService | None = None

    @classmethod
    async def open(
        cls, data_dir: Path, schema: Schema, bridge: Bridge, embedder=None
    ) -> "StoreService":
        data_dir = Path(data_dir)
        conn = await bridge.run(lambda: duckdb.connect(str(data_dir / "duck.db")))
        has_searchable = any(s.searchable for s in schema.tables)
        dim = int(embedder.dim) if (embedder is not None and has_searchable) else None
        engine = cls(bridge, conn, schema, dim)
        await bridge.run(engine._create_tables)
        if dim is not None:
            engine._search = await SearchService.create(bridge, conn, schema, embedder)
        return engine

    @property
    def search(self) -> SearchService | None:
        return self._search

    @property
    def schema(self) -> Schema:
        return self._schema

    # ─── DDL (one physical table per business table; write-once PK) ─────

    def _create_tables(self) -> None:
        for spec in self._schema.tables:
            defs = [f"{_ident(c.name)} {c.sql_type}" for c in spec.columns]
            defs += [f"{_ident(DS)} VARCHAR", f"{_ident(CREATED_AT)} VARCHAR",
                     f"{_ident(DELETED_DS)} VARCHAR", f"{_ident(DELETED_AT)} VARCHAR"]
            if self._dim is not None:
                for col in spec.searchable:
                    defs.append(f"{_ident(veccol(col))} FLOAT[{self._dim}]")
                    defs.append(f"{_ident(tokcol(col))} VARCHAR")
            defs.append(f"PRIMARY KEY ({_ident(spec.primary_key)})")
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {_phys(spec.name)} ({', '.join(defs)})")

    # ─── per-request visibility view (time machine = ds filter) ────────

    def _visible(self, spec: TableSpec, ds_start: str | None, ds_end: str | None) -> str:
        cols = ", ".join(_ident(c) for c in spec.all_column_names)   # business + meta only
        conds: list[str] = []
        if ds_end is None:
            conds.append(f"{_ident(DELETED_DS)} IS NULL")            # current live state
        else:                                                        # as-of ds_end
            conds.append(f"{_ident(DS)} <= '{ds_end}'")
            conds.append(f"({_ident(DELETED_DS)} IS NULL OR {_ident(DELETED_DS)} > '{ds_end}')")
        if ds_start is not None:
            conds.append(f"{_ident(DS)} >= '{ds_start}'")
        return f"SELECT {cols} FROM {_phys(spec.name)} WHERE {' AND '.join(conds)}"

    def _install_views(
        self, ds_start: str | None, ds_end: str | None, searches: list | None = None
    ) -> None:
        searches = searches or []
        by_table: dict[str, list[tuple[str, str]]] = {}
        for table, name, tmp in searches:
            by_table.setdefault(table, []).append((name, tmp))
        single = len(searches) == 1

        for spec in self._schema.tables:
            base = self._visible(spec, ds_start, ds_end)
            ts = by_table.get(spec.name, [])
            if ts:
                pk = _ident(spec.primary_key)
                joins, scores = [], []
                for i, (name, tmp) in enumerate(ts):
                    a = f"_s{i}"
                    joins.append(
                        f'LEFT JOIN {_ident(tmp)} {a} ON CAST(base.{pk} AS VARCHAR) = {a}.pk')
                    scores.append(f"{a}.score AS {_ident(name)}")
                    if single:
                        scores.append(f"{a}.score AS _score")
                body = f"SELECT base.*, {', '.join(scores)} FROM ({base}) base {' '.join(joins)}"
            else:
                body = base
            self._conn.execute(f"CREATE OR REPLACE TEMP VIEW {_ident(spec.name)} AS {body}")

    # ─── read ──────────────────────────────────────────────────────────

    async def run_query(
        self,
        sql: str,
        params: list[Any],
        ds_start: str | None,
        ds_end: str | None,
        searches: list[tuple[str, str, list[tuple[str, float]]]] | None = None,
    ) -> list[dict]:
        _check_ds("ds_start", ds_start)
        _check_ds("ds_end", ds_end)

        def _do() -> list[dict]:
            try:
                stmts = self._conn.extract_statements(sql)
            except duckdb.Error as e:
                raise QueryError(str(e)) from e
            if len(stmts) != 1:
                raise ReadOnlyError("query must be a single read statement")
            if stmts[0].type != duckdb.StatementType.SELECT:
                raise ReadOnlyError("query is read-only (must be a SELECT)")
            try:
                view_searches = []
                for i, (table, name, results) in enumerate(searches or []):
                    tmp = f"_sb_s_{i}"
                    self._conn.execute(
                        f"CREATE OR REPLACE TEMP TABLE {_ident(tmp)} (pk VARCHAR, score DOUBLE)")
                    if results:
                        self._conn.executemany(
                            f"INSERT INTO {_ident(tmp)} VALUES (?, ?)",
                            [[p, sc] for p, sc in results])
                    view_searches.append((table, name, tmp))
                self._install_views(ds_start, ds_end, view_searches)
                cur = self._conn.execute(sql, list(params))
                names = [d[0] for d in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()]
            except duckdb.Error as e:
                raise QueryError(str(e)) from e

        return await self._bridge.run(_do)

    # ─── write primitives (orchestrated by write_service / admin_service) ─

    async def validate(self, table: str, rows: list[dict]) -> list[dict]:
        """Validate rows for insert — unknown columns + write-once primary key
        (intra-batch and against existing rows) — and return the normalized
        records (business columns only). The PRIMARY KEY constraint backstops
        the dup check if two inserts race past it."""
        spec = self._schema.table(table)
        records: list[dict] = []
        for row in rows:
            unknown = set(row) - set(spec.column_names)
            if unknown:
                raise QueryError(f"{table}: unknown column(s) {sorted(unknown)}")
            records.append({c: row.get(c) for c in spec.column_names})
        keys = [str(r[spec.primary_key]) for r in records]
        if len(set(keys)) != len(keys):
            raise QueryError(f"{table}: duplicate primary key within the insert batch")
        existing = await self._existing_keys(table, keys)
        if existing:
            raise QueryError(
                f"{table}: primary key already exists: {existing[0]!r} "
                f"(seekbase is insert-only; a key is written once)")
        return records

    async def _existing_keys(self, table: str, keys: list[str]) -> list[str]:
        if not keys:
            return []
        pk = self._schema.table(table).primary_key
        ph = ", ".join("?" * len(keys))
        return await self._bridge.run(lambda: [r[0] for r in self._conn.execute(
            f"SELECT CAST({_ident(pk)} AS VARCHAR) FROM {_phys(table)} "
            f"WHERE CAST({_ident(pk)} AS VARCHAR) IN ({ph})", keys).fetchall()])

    def _insert_rows(self, spec: TableSpec, records: list[dict], vecs: dict, toks: dict,
                     ds: str, now: str) -> None:
        json_cols = {c.name for c in spec.columns if c.type == "json"}
        cols = [*spec.column_names, DS, CREATED_AT, DELETED_DS, DELETED_AT]
        if self._dim is not None:
            for col in spec.searchable:
                cols += [veccol(col), tokcol(col)]
        col_sql = ", ".join(_ident(c) for c in cols)
        placeholders = ", ".join("?" * len(cols))
        sql = f"INSERT INTO {_phys(spec.name)} ({col_sql}) VALUES ({placeholders})"
        payload: list[list[Any]] = []
        for i, rec in enumerate(records):
            vals: list[Any] = []
            for c in spec.column_names:
                v = rec.get(c)
                if c in json_cols and v is not None:
                    v = json.dumps(v)
                vals.append(v)
            vals += [ds, now, None, None]
            if self._dim is not None:
                for col in spec.searchable:
                    vals.append(vecs.get(col, [None] * len(records))[i])
                    vals.append(toks.get(col, [None] * len(records))[i])
            payload.append(vals)
        self._conn.executemany(sql, payload)

    async def commit_rows(self, spec: TableSpec, records: list[dict], vecs: dict, toks: dict,
                          ds: str, now: str, *, rebuild_fts: bool = True) -> None:
        """INSERT rows (vectors/tokens included) and, by default, refresh the
        table's FTS index — in one bridge block, so a write settles atomically."""
        def _db() -> None:
            self._insert_rows(spec, records, vecs, toks, ds, now)
            if rebuild_fts and self._search is not None:
                self._search.rebuild_fts_inline(spec.name)
        await self._bridge.run(_db)

    async def rebuild_fts(self, table: str) -> None:
        if self._search is not None:
            await self._bridge.run(lambda: self._search.rebuild_fts_inline(table))

    async def match_live(self, table: str, where: str, params: list[Any]) -> list:
        """Primary keys of the currently-live rows matching ``where`` (used by
        delete to resolve targets)."""
        spec = self._schema.table(table)
        stmt = _one_statement(where, "delete where")
        pk = spec.primary_key

        def _do() -> list:
            try:
                self._install_views(None, None, None)   # current live state
                cur = self._conn.execute(
                    f"SELECT {_ident(pk)} FROM {_ident(table)} WHERE ({stmt})", list(params))
                return [r[0] for r in cur.fetchall()]
            except duckdb.Error as e:
                raise QueryError(f"{table}: bad delete where: {e}") from e

        return await self._bridge.run(_do)

    async def soft_delete(self, table: str, keys: list, ds: str, now: str) -> None:
        if not keys:
            return
        pk = self._schema.table(table).primary_key
        ph = ", ".join("?" * len(keys))
        await self._bridge.run(lambda: self._conn.execute(
            f"UPDATE {_phys(table)} SET {_ident(DELETED_DS)}=?, {_ident(DELETED_AT)}=? "
            f"WHERE CAST({_ident(pk)} AS VARCHAR) IN ({ph}) AND {_ident(DELETED_DS)} IS NULL",
            [ds, now, *[str(k) for k in keys]]))

    async def clear(self, table: str) -> None:
        await self._bridge.run(lambda: self._conn.execute(f"DELETE FROM {_phys(table)}"))

    async def close(self) -> None:
        await self._bridge.run(self._conn.close)
