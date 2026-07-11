"""DuckdbEngine — the single engine (structured + vss + fts).

One **physical table per business table**, ``_sb_<table>``: business columns +
engine metadata (``ds`` / ``created_at`` / ``deleted_ds`` / ``deleted_at``) +,
per searchable column, ``_vec_<col>`` (HNSW) and ``_tok_<col>`` (BM25). The
primary key is **write-once**: re-inserting an existing key is rejected (so a
row — and its vector — is set once and never updated, only soft-deleted).

- insert → INSERT one row (business + ds/created_at + inline embedding/tokens).
- delete → UPDATE ``deleted_ds`` / ``deleted_at`` on the row (soft delete).
- query → a per-request view over the table with the visibility predicate
  (``deleted_ds IS NULL`` now, or the ``ds`` / delete-horizon window for a time
  machine). ``query`` is strictly read-only.

Time machine = Hive-style ``ds`` partition filtering: a row is visible as-of
``ds_end`` iff ``ds <= ds_end`` and it was not yet deleted then. No event log,
no reconstruction — keys are write-once, so one row per key is the whole story.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from .._types import QueryError, ReadOnlyError
from ..schema import CREATED_AT, DELETED_AT, DELETED_DS, DS, Schema, TableSpec
from .bridge import Bridge
from .files import FileMirror
from .search import SearchEngine, tokcol, veccol

__all__ = ["DuckdbEngine", "extract_searches", "search_target"]

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DS_RE = re.compile(r"^\d{8}$")
_SEARCH_RE = re.compile(
    r"search\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*'((?:[^']|'')*)'\s*\)", re.IGNORECASE)


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise QueryError(f"illegal identifier {name!r}")
    return f'"{name}"'


def _phys(name: str) -> str:
    return _ident(f"_sb_{name}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_ds() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _check_ds(label: str, value: str | None) -> None:
    if value is not None and not _DS_RE.match(value):
        raise QueryError(f"{label} must be YYYYMMDD, got {value!r}")


def _one_statement(sql: str, label: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        raise QueryError(f"{label} must be a single statement")
    return s


def extract_searches(sql: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Replace each ``search(column, 'literal')`` with a boolean referencing its
    score column, and return (rewritten sql, [(column, text, score_col), …]).
    The score column is ``_score_<column>`` (deduped on collision)."""
    specs: list[tuple[str, str, str]] = []
    used: set[str] = set()

    def _repl(m: re.Match) -> str:
        col, text = m.group(1), m.group(2).replace("''", "'")
        name = f"_score_{col}"
        i = 1
        while name in used:
            i += 1
            name = f"_score_{col}_{i}"
        used.add(name)
        specs.append((col, text, name))
        return f'({_ident(name)} IS NOT NULL)'

    return _SEARCH_RE.sub(_repl, sql), specs


def search_target(schema: Schema, sql: str, col: str) -> str:
    """The single table referenced by ``sql`` that has ``col`` as searchable."""
    hits = [t.name for t in schema.tables
            if col in t.searchable and re.search(rf"\b{re.escape(t.name)}\b", sql)]
    if len(hits) != 1:
        raise QueryError(
            f"search({col}, …) needs exactly one table with a searchable "
            f"{col!r} column in the query")
    return hits[0]


class DuckdbEngine:
    def __init__(self, bridge: Bridge, conn, schema: Schema, mirror: FileMirror,
                 dim: int | None) -> None:
        self._bridge = bridge
        self._conn = conn
        self._schema = schema
        self._mirror = mirror
        self._dim = dim               # embedding dim, or None if nothing searchable
        self._search: SearchEngine | None = None

    @classmethod
    async def open(
        cls, data_dir: Path, schema: Schema, bridge: Bridge, embedder=None
    ) -> "DuckdbEngine":
        data_dir = Path(data_dir)
        conn = await bridge.run(lambda: duckdb.connect(str(data_dir / "duck.db")))
        has_searchable = any(s.searchable for s in schema.tables)
        dim = int(embedder.dim) if (embedder is not None and has_searchable) else None
        engine = cls(bridge, conn, schema, FileMirror(data_dir / "files"), dim)
        await bridge.run(engine._create_tables)
        if dim is not None:
            engine._search = await SearchEngine.create(bridge, conn, schema, embedder)
        return engine

    @property
    def search(self) -> SearchEngine | None:
        return self._search

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

    async def query(
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

    # ─── write (files-first; insert-once, soft-delete) ─────────────────

    async def _embed_records(self, spec: TableSpec, records: list[dict]) -> tuple[dict, dict]:
        """Inline embed + tokenize each searchable column. Returns
        (vecs, toks): col -> list aligned with records (None where empty)."""
        vecs: dict[str, list] = {}
        toks: dict[str, list] = {}
        if self._search is None:
            return vecs, toks
        for col in spec.searchable:
            texts = [rec.get(col) for rec in records]
            idx = [i for i, t in enumerate(texts) if t is not None and str(t) != ""]
            emb = await self._search.embed([str(texts[i]) for i in idx]) if idx else []
            cv: list = [None] * len(records)
            ct: list = [None] * len(records)
            for j, i in enumerate(idx):
                cv[i] = emb[j]
                ct[i] = self._search.tok(str(texts[i]))
            vecs[col], toks[col] = cv, ct
        return vecs, toks

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

    async def insert(self, table: str, rows: list[dict], ticket: str) -> None:
        spec = self._schema.table(table)
        now, ds = _utc_now(), _today_ds()
        pk = spec.primary_key
        records: list[dict] = []
        for row in rows:
            unknown = set(row) - set(spec.column_names)
            if unknown:
                raise QueryError(f"{table}: unknown column(s) {sorted(unknown)}")
            records.append({c: row.get(c) for c in spec.column_names})

        keys = [str(r[pk]) for r in records]
        if len(set(keys)) != len(keys):
            raise QueryError(f"{table}: duplicate primary key within the insert batch")
        existing = await self._bridge.run(lambda: self._conn.execute(
            f"SELECT CAST({_ident(pk)} AS VARCHAR) FROM {_phys(table)} "
            f"WHERE CAST({_ident(pk)} AS VARCHAR) IN ({', '.join('?' * len(keys))})",
            keys).fetchall())
        if existing:
            raise QueryError(
                f"{table}: primary key already exists: {existing[0][0]!r} "
                f"(seekbase is insert-only; a key is written once)")

        vecs, toks = await self._embed_records(spec, records)
        mrecs = [{**{c: rec[c] for c in spec.column_names}, DS: ds, CREATED_AT: now}
                 for rec in records]
        await self._bridge.run(lambda: [self._mirror.append(ds, table, m) for m in mrecs])

        def _db() -> None:
            self._insert_rows(spec, records, vecs, toks, ds, now)
            if self._search is not None:
                self._search.rebuild_fts_inline(table)

        await self._bridge.run(_db)

    async def tombstone(self, table: str, where: str, params: list[Any], ticket: str) -> int:
        spec = self._schema.table(table)
        stmt = _one_statement(where, "delete where")
        now, ds = _utc_now(), _today_ds()
        pk = spec.primary_key

        def _do() -> int:
            try:
                self._install_views(None, None, None)   # match against current live state
                cur = self._conn.execute(
                    f"SELECT {_ident(pk)} FROM {_ident(table)} WHERE ({stmt})", list(params))
                keys = [r[0] for r in cur.fetchall()]
                for k in keys:
                    self._mirror.append(ds, table, {"_deleted": k, DELETED_AT: now})
                if keys:
                    ph = ", ".join("?" * len(keys))
                    self._conn.execute(
                        f"UPDATE {_phys(table)} SET {_ident(DELETED_DS)}=?, {_ident(DELETED_AT)}=? "
                        f"WHERE CAST({_ident(pk)} AS VARCHAR) IN ({ph}) "
                        f"AND {_ident(DELETED_DS)} IS NULL",
                        [ds, now, *[str(k) for k in keys]])
                return len(keys)
            except duckdb.Error as e:
                raise QueryError(f"{table}: bad delete where: {e}") from e

        return await self._bridge.run(_do)

    # ─── rebuild (replay the file mirror in ds order) ──────────────────

    async def rebuild(self) -> dict:
        result = {"tables": 0, "rows": 0, "tombstones": 0}
        # 1) read events off the bridge (filesystem), then embed puts inline
        replay: dict[str, dict] = {}
        for spec in self._schema.tables:
            puts, dels = [], []
            for ds, rec in self._mirror.iter_events(spec.name):
                if "_deleted" in rec:
                    dels.append((str(rec["_deleted"]), ds, rec.get(DELETED_AT)))
                else:
                    puts.append(rec)
            recs = [{c: p.get(c) for c in spec.column_names} for p in puts]
            vecs, toks = await self._embed_records(spec, recs)
            replay[spec.name] = {
                "recs": recs, "vecs": vecs, "toks": toks, "dels": dels,
                "ds": [p.get(DS) for p in puts],
                "ca": [p.get(CREATED_AT) for p in puts],
            }

        def _do() -> dict:
            for spec in self._schema.tables:
                self._conn.execute(f"DELETE FROM {_phys(spec.name)}")
            for spec in self._schema.tables:
                result["tables"] += 1
                r = replay[spec.name]
                for i, rec in enumerate(r["recs"]):
                    self._insert_rows(
                        spec, [rec],
                        {c: [r["vecs"][c][i]] for c in r["vecs"]},
                        {c: [r["toks"][c][i]] for c in r["toks"]},
                        r["ds"][i], r["ca"][i] or _utc_now())
                    result["rows"] += 1
                for pk_val, dds, dat in r["dels"]:
                    self._conn.execute(
                        f"UPDATE {_phys(spec.name)} SET {_ident(DELETED_DS)}=?, {_ident(DELETED_AT)}=? "
                        f"WHERE CAST({_ident(spec.primary_key)} AS VARCHAR)=? "
                        f"AND {_ident(DELETED_DS)} IS NULL",
                        [dds, dat, pk_val])
                    result["tombstones"] += 1
                if self._search is not None:
                    self._search.rebuild_fts_inline(spec.name)
            return result

        return await self._bridge.run(_do)

    async def close(self) -> None:
        await self._bridge.run(self._conn.close)

    @property
    def schema(self) -> Schema:
        return self._schema
