"""StoreService — the structured-storage subdomain (DuckDB).

Owns the DuckDB side: one physical table per business table, ``_sb_<table>``,
with business columns + metadata (ds / created_at / deleted_ds / deleted_at) +,
per searchable column, ``_vec_<col>`` (HNSW) and ``_tok_<col>`` (BM25). The
primary key is write-once. It owns structured validation (unknown columns,
dup-pk), the row primitives (``commit_rows`` / ``match_live`` / ``soft_delete``
/ ``clear``) and read (``run_query`` — read-only guard + visibility view +
search-score joins) and the vss+fts indexes on those same tables — one
DuckDB engine, one connection: ``commit_rows`` refreshes the touched FTS index
in the same block; ``hybrid`` runs the RRF query (given a precomputed vector).

The use-case services sequence StoreService with FileService and
EmbeddingService (which turns text into the vectors/tokens the store persists).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import duckdb

from .._types import QueryError, ReadOnlyError
from ..runtime import Bridge, ReadPool
from ..struct import CREATED_AT, DELETED_AT, DELETED_DS, DS, Schema, TableSpec

__all__ = ["StoreService"]

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DS_RE = re.compile(r"^\d{8}$")
_SEARCH_K = 100          # candidates per arm (vss / fts) before RRF


def veccol(col: str) -> str:
    return f"_vec_{col}"


def tokcol(col: str) -> str:
    return f"_tok_{col}"


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
        self._bridge = bridge          # single-writer: all writes serialize here
        self._conn = conn
        self._schema = schema
        self._dim = dim               # embedding dim, or None if nothing searchable
        self._reads: ReadPool | None = None   # concurrent reads (cursors), off the write bridge

    @classmethod
    async def open(
        cls, data_dir: Path, schema: Schema, bridge: Bridge, dim: int | None = None
    ) -> "StoreService":
        data_dir = Path(data_dir)
        conn = await bridge.run(lambda: duckdb.connect(str(data_dir / "duck.db")))
        engine = cls(bridge, conn, schema, dim)
        await bridge.run(engine._create_tables)
        if dim is not None:
            await bridge.run(engine._setup_search)   # vss + fts extensions + indexes
        engine._reads = await ReadPool.create(bridge, conn)   # read cursors off the write bridge
        return engine

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

    # ─── vss + fts on the business tables (same connection, single engine) ─

    def _setup_search(self) -> None:
        for ext in ("vss", "fts"):
            try:
                self._conn.execute(f"INSTALL {ext}; LOAD {ext};")
            except Exception:
                self._conn.execute(f"LOAD {ext};")   # already installed offline
        self._conn.execute("SET hnsw_enable_experimental_persistence=true;")
        for spec in self._schema.tables:
            if not spec.searchable:
                continue
            phys = f"_sb_{spec.name}"
            for col in spec.searchable:
                self._conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "{phys}_{col}_hnsw" ON "{phys}" '
                    f"USING HNSW(\"{veccol(col)}\") WITH (metric='cosine')")
            self._build_fts(spec)

    def _build_fts(self, spec: TableSpec) -> None:
        phys = f"_sb_{spec.name}"
        cols = ", ".join(f"'{tokcol(c)}'" for c in spec.searchable)
        self._conn.execute(
            f"PRAGMA create_fts_index('{phys}', '{spec.primary_key}', {cols}, overwrite=1)")

    def _rebuild_fts_inline(self, table: str) -> None:
        """Refresh a table's BM25 index. Called inside an existing bridge block."""
        spec = self._schema.table(table)
        if spec.searchable:
            self._build_fts(spec)

    async def hybrid(self, table: str, col: str, qvec: list[float], qtok: str,
                     k: int = _SEARCH_K) -> list[tuple[str, float]]:
        """Fuse vss (cosine ANN over ``qvec``) + fts (BM25 over ``qtok``) by RRF
        (k0=60) on the given column. The query vector/tokens are precomputed by
        EmbeddingService; this is pure DuckDB."""
        spec = self._schema.table(table)
        phys = f"_sb_{table}"
        pk = spec.primary_key
        f = f"fts_main_{phys}"
        vc, tc = veccol(col), tokcol(col)
        sql = (
            f'WITH v AS (SELECT pk, row_number() OVER (ORDER BY dd) rk FROM '
            f'(SELECT "{pk}" pk, array_cosine_distance("{vc}", ?::FLOAT[{self._dim}]) dd '
            f'FROM "{phys}" WHERE "{vc}" IS NOT NULL AND "deleted_ds" IS NULL '
            f'ORDER BY dd LIMIT {k})), '
            f"ff AS (SELECT pk, row_number() OVER (ORDER BY sc DESC) rk FROM "
            f"(SELECT \"{pk}\" pk, {f}.match_bm25(\"{pk}\", ?, fields := '{tc}') sc "
            f'FROM "{phys}" WHERE {f}.match_bm25("{pk}", ?, fields := \'{tc}\') IS NOT NULL '
            f'AND "deleted_ds" IS NULL ORDER BY sc DESC LIMIT {k})) '
            f"SELECT COALESCE(v.pk, ff.pk) pk, "
            f"COALESCE(1.0/(60+v.rk),0)+COALESCE(1.0/(60+ff.rk),0) score "
            f"FROM v FULL OUTER JOIN ff ON v.pk=ff.pk ORDER BY score DESC LIMIT {k}")

        def _do():
            return self._conn.execute(sql, [qvec, qtok, qtok]).fetchall()

        rows = await self._bridge.run(_do)
        return [(str(pk_), float(sc)) for pk_, sc in rows]

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
        self, conn, ds_start: str | None, ds_end: str | None, searches: list | None = None
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
            conn.execute(f"CREATE OR REPLACE TEMP VIEW {_ident(spec.name)} AS {body}")

    # ─── read (on the ReadPool: a cursor, concurrent, off the write bridge) ─

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

        def _do(conn) -> list[dict]:            # conn = a borrowed read cursor
            try:
                stmts = conn.extract_statements(sql)
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
                    conn.execute(
                        f"CREATE OR REPLACE TEMP TABLE {_ident(tmp)} (pk VARCHAR, score DOUBLE)")
                    if results:
                        conn.executemany(
                            f"INSERT INTO {_ident(tmp)} VALUES (?, ?)",
                            [[p, sc] for p, sc in results])
                    view_searches.append((table, name, tmp))
                self._install_views(conn, ds_start, ds_end, view_searches)
                cur = conn.execute(sql, list(params))
                names = [d[0] for d in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()]
            except duckdb.Error as e:
                raise QueryError(str(e)) from e

        return await self._reads.run(_do)

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
            if rebuild_fts and self._dim is not None:
                self._rebuild_fts_inline(spec.name)
        await self._bridge.run(_db)

    async def rebuild_fts(self, table: str) -> None:
        if self._dim is not None:
            await self._bridge.run(lambda: self._rebuild_fts_inline(table))

    async def match_live(self, table: str, where: str, params: list[Any]) -> list:
        """Primary keys of the currently-live rows matching ``where`` (used by
        delete to resolve targets)."""
        spec = self._schema.table(table)
        stmt = _one_statement(where, "delete where")
        pk = spec.primary_key

        def _do() -> list:
            try:
                self._install_views(self._conn, None, None, None)   # current live state
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
        if self._reads is not None:
            self._reads.close()                  # stop read threads (+ their cursors)
        await self._bridge.run(self._conn.close)
