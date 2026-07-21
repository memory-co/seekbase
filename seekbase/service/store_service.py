"""StoreService — the structured-storage subdomain (DuckDB).

Owns the DuckDB side: one physical table per business table, ``_sb_<table>``,
with business columns + metadata (ds / created_at / deleted_ds / deleted_at) +,
per searchable column, ``_vec_<col>`` (HNSW) and ``_tok_<col>`` (BM25). The
primary key is write-once. It owns structured validation (unknown columns,
dup-pk), the row primitives (``commit_rows`` / ``match_live`` / ``soft_delete``
/ ``clear``) and read (``run_query`` — read-only guard + visibility views),
plus the **pluggable search backend** (docs/works/search.md): ``vss`` keeps
vec/tok columns in the duck rows (HNSW + FTS on the tables, refreshed by
``commit_rows``); ``lance`` keeps them in side LanceDB datasets reached via
the DuckDB ``lance`` extension (COPY appends + ``lance_*`` table functions) —
both through this one connection, which is why the backend lives here rather
than in a separate service. ``search_lower`` is the SQL knowledge behind the
pipeline's ``search`` source: RRF (k0=60) shared across backends, only the
candidate arms differ.

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
_OVERFETCH = 4           # as-of search: widen the arm's candidate pool so post-
#                          filtering the ds/deleted horizon still yields ~k live
#                          rows (HNSW/BM25 filter *after* top-k → under-returns).


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


def _asof_conds(ds_start: str | None, ds_end: str | None) -> list[str]:
    """The visibility predicate (time machine = ds filter), shared by the
    structured read view (``_visible``) and search candidate generation
    (``hybrid``) so query and search agree on "what's alive as-of D".
    ``ds_end`` None → current live state; set → as-of that day."""
    conds: list[str] = []
    if ds_end is None:
        conds.append(f"{_ident(DELETED_DS)} IS NULL")            # current live state
    else:                                                        # as-of ds_end
        conds.append(f"{_ident(DS)} <= '{ds_end}'")
        conds.append(f"({_ident(DELETED_DS)} IS NULL OR {_ident(DELETED_DS)} > '{ds_end}')")
    if ds_start is not None:
        conds.append(f"{_ident(DS)} >= '{ds_start}'")
    return conds


def _check_ds(label: str, value: str | None) -> None:
    if value is not None and not _DS_RE.match(value):
        raise QueryError(f"{label} must be YYYYMMDD, got {value!r}")


def _one_statement(sql: str, label: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        raise QueryError(f"{label} must be a single statement")
    return s


def _l2_normalize(v: list[float]) -> list[float]:
    """Unit-normalize, so lance's L2 ``_distance`` ranks identically to the vss
    backend's cosine (‖a−b‖² = 2−2cosθ on unit vectors — monotonic)."""
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v] if n else list(v)


class StoreService:
    def __init__(self, bridge: Bridge, conn, schema: Schema, dim: int | None,
                 search_backend: str = "vss", lance_dir: Path | None = None) -> None:
        self._bridge = bridge          # single-writer: all writes serialize here
        self._conn = conn
        self._schema = schema
        self._dim = dim               # embedding dim, or None if nothing searchable
        self._backend = search_backend   # "vss" (in-table) | "lance" (side datasets)
        self._lance_dir = lance_dir
        self._reads: ReadPool | None = None   # concurrent reads (cursors), off the write bridge

    @classmethod
    async def open(
        cls, data_dir: Path, schema: Schema, bridge: Bridge, dim: int | None = None,
        search_backend: str = "vss",
    ) -> "StoreService":
        if search_backend not in ("vss", "lance"):
            raise QueryError(f"unknown search backend {search_backend!r} (vss | lance)")
        data_dir = Path(data_dir)
        conn = await bridge.run(lambda: duckdb.connect(str(data_dir / "duck.db")))
        engine = cls(bridge, conn, schema, dim, search_backend, data_dir / "lance")
        await bridge.run(engine._create_tables)
        if engine._vss:
            await bridge.run(engine._setup_search)   # vss + fts extensions + indexes
        elif engine._lance:
            await bridge.run(engine._setup_lance)    # lance extension + one dataset per table
        engine._reads = await ReadPool.create(bridge, conn)   # read cursors off the write bridge
        return engine

    @property
    def _vss(self) -> bool:
        """Search columns live *in* the duck rows (vss + fts on the tables)."""
        return self._backend == "vss" and self._dim is not None

    @property
    def _lance(self) -> bool:
        """Search rows live in side LanceDB datasets (via the duck lance ext)."""
        return self._backend == "lance" and self._dim is not None

    @property
    def schema(self) -> Schema:
        return self._schema

    # ─── DDL (one physical table per business table; write-once PK) ─────

    def _create_tables(self) -> None:
        for spec in self._schema.tables:
            defs = [f"{_ident(c.name)} {c.sql_type}" for c in spec.columns]
            defs += [f"{_ident(DS)} VARCHAR", f"{_ident(CREATED_AT)} VARCHAR",
                     f"{_ident(DELETED_DS)} VARCHAR", f"{_ident(DELETED_AT)} VARCHAR"]
            if self._vss:
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

    # ─── LanceDB backend (side datasets, via the DuckDB `lance` extension) ─
    #
    # The whole backend goes through the same duck connection: writes are
    # ``COPY … (FORMAT lance)`` appends, queries are the ``lance_*`` table
    # functions — no separate SDK/connection. The datasets carry only
    # (pk, vec/tok per searchable column): visibility (ds/deleted) is applied
    # by joining hits back to the duck visible view, so soft-deleted rows stay
    # in the index and the candidate pool is always widened ``_OVERFETCH``×
    # (the arms cannot pre-filter). Vectors are L2-normalized on both the
    # index and query side, so lance's L2 ``_distance`` ranks like cosine.

    def _lance_ds(self, table: str) -> str:
        return str(self._lance_dir / f"{table}.lance")

    def _lance_stage_cols(self, spec: TableSpec) -> list[str]:
        cols = ["pk VARCHAR"]
        for col in spec.searchable:
            cols += [f'"vec_{col}" FLOAT[{self._dim}]', f'"tok_{col}" VARCHAR']
        return cols

    def _setup_lance(self) -> None:
        try:
            self._conn.execute("INSTALL lance; LOAD lance;")
        except Exception:
            self._conn.execute("LOAD lance;")        # already installed offline
        self._lance_dir.mkdir(parents=True, exist_ok=True)
        for spec in self._schema.tables:
            if spec.searchable and not Path(self._lance_ds(spec.name)).exists():
                self._lance_write(spec, [], mode="overwrite")   # empty dataset: searchable from day 0

    def _lance_write(self, spec: TableSpec, payload: list[list], mode: str) -> None:
        """Append/overwrite index rows via a staging temp table + COPY.
        Runs inside a bridge block (write connection)."""
        cols = self._lance_stage_cols(spec)
        self._conn.execute(f"CREATE OR REPLACE TEMP TABLE _lance_stage ({', '.join(cols)})")
        if payload:
            ph = ", ".join("?" * len(cols))
            self._conn.executemany(f"INSERT INTO _lance_stage VALUES ({ph})", payload)
        self._conn.execute(
            f"COPY (SELECT * FROM _lance_stage) TO '{self._lance_ds(spec.name)}' "
            f"(FORMAT lance, mode '{mode}')")
        self._conn.execute("DROP TABLE _lance_stage")

    def _lance_payload(self, spec: TableSpec, records: list[dict], vecs: dict, toks: dict) -> list[list]:
        pk = spec.primary_key
        out: list[list] = []
        for i, rec in enumerate(records):
            vals: list = [str(rec[pk])]
            for col in spec.searchable:
                v = vecs.get(col, [None] * len(records))[i]
                vals.append(_l2_normalize(v) if v is not None else None)
                vals.append(toks.get(col, [None] * len(records))[i])
            out.append(vals)
        return out

    async def index_search_rows(self, spec: TableSpec, records: list[dict],
                                vecs: dict, toks: dict) -> None:
        """Write a batch's search-index rows. vss: no-op (the vectors live in
        the duck rows, written by ``commit_rows``); lance: append to the
        table's side dataset. Ordered after ``commit_rows`` in the write flow —
        a lance append failure surfaces to the caller and ``rebuild`` heals."""
        if not self._lance or not spec.searchable or not records:
            return
        payload = self._lance_payload(spec, records, vecs, toks)
        await self._bridge.run(lambda: self._lance_write(spec, payload, mode="append"))

    async def rebuild_search_index(self, spec: TableSpec, records: list[dict],
                                   vecs: dict, toks: dict) -> None:
        """Rebuild a table's search index wholesale (admin rebuild). vss: no-op
        (``rebuild_fts`` covers it); lance: one overwrite COPY — also resets
        the dataset's fragment count."""
        if not self._lance or not spec.searchable:
            return
        payload = self._lance_payload(spec, records, vecs, toks)
        await self._bridge.run(lambda: self._lance_write(spec, payload, mode="overwrite"))

    # ─── search lowering (the pipeline `search` source; backend-branched) ─

    def search_lower(self, table: str, col: str, qvec: list[float], qtok: str,
                     k: int = _SEARCH_K, ds_start: str | None = None,
                     ds_end: str | None = None) -> tuple[str, list]:
        """The ``search`` source stage's duck lowering: one SQL (a CTE body)
        fusing vector ANN + BM25 by RRF (k0=60) on ``col``, joined back to the
        table's visible columns and emitting them plus ``_score``. Returns
        ``(sql, params)``; the RRF fusion is shared across backends — only the
        candidate arms differ (docs/works/search.md §3)."""
        if self._vss:
            return self._vss_search_sql(table, col, k, ds_start, ds_end), [qvec, qtok, qtok]
        if self._lance:
            return (self._lance_search_sql(table, col, k, ds_start, ds_end),
                    [_l2_normalize(qvec), qtok])
        raise QueryError("search needs a searchable column + an embedder")

    def _vss_search_sql(self, table: str, col: str, k: int,
                        ds_start: str | None, ds_end: str | None) -> str:
        """vss+fts arms over the in-table columns. Both arms filter by the
        *same* as-of predicate as the structured read (``_asof_conds``), so
        search and query agree on what's alive as-of D. The HNSW/BM25 filter is
        applied *after* each arm's top-k, so a live-as-of-D row can be crowded
        out by now-live-but-not-then rows; on the historical path the candidate
        pool is widened ``_OVERFETCH``×. Params: [qvec, qtok, qtok]."""
        spec = self._schema.table(table)
        phys = f"_sb_{table}"
        pk = spec.primary_key
        f = f"fts_main_{phys}"
        vc, tc = veccol(col), tokcol(col)
        vis = " AND ".join(_asof_conds(ds_start, ds_end))
        cand = k * _OVERFETCH if ds_end is not None else k       # now-path unchanged
        return (
            f'WITH _v AS (SELECT pk, row_number() OVER (ORDER BY dd) rk FROM '
            f'(SELECT "{pk}" pk, array_cosine_distance("{vc}", ?::FLOAT[{self._dim}]) dd '
            f'FROM "{phys}" WHERE "{vc}" IS NOT NULL AND {vis} '
            f'ORDER BY dd LIMIT {cand})), '
            f"_f AS (SELECT pk, row_number() OVER (ORDER BY sc DESC) rk FROM "
            f"(SELECT \"{pk}\" pk, {f}.match_bm25(\"{pk}\", ?, fields := '{tc}') sc "
            f'FROM "{phys}" WHERE {f}.match_bm25("{pk}", ?, fields := \'{tc}\') IS NOT NULL '
            f'AND {vis} ORDER BY sc DESC LIMIT {cand})), '
            f"_h AS (SELECT COALESCE(_v.pk, _f.pk) pk, "
            f"COALESCE(1.0/(60+_v.rk),0)+COALESCE(1.0/(60+_f.rk),0) score "
            f"FROM _v FULL OUTER JOIN _f ON _v.pk=_f.pk ORDER BY score DESC LIMIT {k}) "
            f'SELECT base.*, _h.score AS _score '
            f"FROM ({self._visible(spec, ds_start, ds_end)}) base "
            f'JOIN _h ON CAST(base.{_ident(pk)} AS VARCHAR) = CAST(_h.pk AS VARCHAR)')

    def _lance_search_sql(self, table: str, col: str, k: int,
                          ds_start: str | None, ds_end: str | None) -> str:
        """lance_vector_search + lance_fts arms over the side dataset, same RRF.
        The arms carry no visibility predicate (the dataset has no ds columns),
        so the as-of filter is applied by the join to the visible view and the
        candidate pool is *always* widened ``_OVERFETCH``× to compensate.
        Params: [qvec_normalized, qtok]."""
        spec = self._schema.table(table)
        pk = spec.primary_key
        ds = self._lance_ds(table)
        cand = k * _OVERFETCH                                     # arms can't pre-filter
        return (
            f"WITH _v AS (SELECT pk, row_number() OVER (ORDER BY _distance) rk "
            f"FROM lance_vector_search('{ds}', 'vec_{col}', ?::FLOAT[{self._dim}], k={cand})), "
            f"_f AS (SELECT pk, row_number() OVER (ORDER BY _score DESC) rk "
            f"FROM lance_fts('{ds}', 'tok_{col}', ?, k={cand})), "
            f"_h AS (SELECT COALESCE(_v.pk, _f.pk) pk, "
            f"COALESCE(1.0/(60+_v.rk),0)+COALESCE(1.0/(60+_f.rk),0) score "
            f"FROM _v FULL OUTER JOIN _f ON _v.pk=_f.pk) "
            f'SELECT base.*, _h.score AS _score '
            f"FROM ({self._visible(spec, ds_start, ds_end)}) base "
            f'JOIN _h ON CAST(base.{_ident(pk)} AS VARCHAR) = _h.pk '
            f"ORDER BY _score DESC LIMIT {k}")

    def visible_sql(self, table: str, ds_start: str | None, ds_end: str | None) -> str:
        """The table's visibility view SQL (the ``scan`` source's duck lowering)."""
        return self._visible(self._schema.table(table), ds_start, ds_end)

    # ─── per-request visibility view (time machine = ds filter) ────────

    def _visible(self, spec: TableSpec, ds_start: str | None, ds_end: str | None) -> str:
        cols = ", ".join(_ident(c) for c in spec.all_column_names)   # business + meta only
        conds = _asof_conds(ds_start, ds_end)
        return f"SELECT {cols} FROM {_phys(spec.name)} WHERE {' AND '.join(conds)}"

    def _install_views(self, conn, ds_start: str | None, ds_end: str | None) -> None:
        for spec in self._schema.tables:
            conn.execute(
                f"CREATE OR REPLACE TEMP VIEW {_ident(spec.name)} AS "
                f"{self._visible(spec, ds_start, ds_end)}")

    # ─── read (on the ReadPool: a cursor, concurrent, off the write bridge) ─

    async def run_query(
        self,
        sql: str,
        params: list[Any],
        ds_start: str | None,
        ds_end: str | None,
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
                self._install_views(conn, ds_start, ds_end)
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
        if self._vss:
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
            if self._vss:
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
            if rebuild_fts and self._vss:
                self._rebuild_fts_inline(spec.name)
        await self._bridge.run(_db)

    async def rebuild_fts(self, table: str) -> None:
        if self._vss:
            await self._bridge.run(lambda: self._rebuild_fts_inline(table))

    async def match_live(self, table: str, where: str, params: list[Any]) -> list:
        """Primary keys of the currently-live rows matching ``where`` (used by
        delete to resolve targets)."""
        spec = self._schema.table(table)
        stmt = _one_statement(where, "delete where")
        pk = spec.primary_key

        def _do() -> list:
            try:
                self._install_views(self._conn, None, None)   # current live state
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
