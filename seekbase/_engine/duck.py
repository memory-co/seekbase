"""DuckdbEngine — the structured/analytical engine (append-only).

DuckDB storage is **insert-only, like the files**: every write is a new event
row, never an UPDATE or REPLACE.
- insert → a *put* event (business columns + ds + created_at, `_seq`).
- delete → a *del* event (pk + deleted_ds + deleted_at, `_seq`); business
  columns NULL, ds NULL.
- re-insert of a pk → just another put event (a new version).

`query` reads a per-request **reconstruction view**: for each pk, take the
latest event (`_seq`) whose day ≤ the horizon; the row is live iff that event is
a put. This makes the time machine complete for arbitrary create/delete/re-
insert histories, and query is strictly read-only.
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

__all__ = ["DuckdbEngine", "extract_searches", "search_target"]

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DS_RE = re.compile(r"^\d{8}$")
_SEARCH_RE = re.compile(
    r"search\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*'((?:[^']|'')*)'\s*\)", re.IGNORECASE)
_SEQ = "_seq"


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
    def __init__(self, bridge: Bridge, conn, schema: Schema, mirror: FileMirror) -> None:
        self._bridge = bridge
        self._conn = conn
        self._schema = schema
        self._mirror = mirror
        self._search = None            # SearchEngine (vss+fts) or None if no searchable

    @classmethod
    async def open(
        cls, data_dir: Path, schema: Schema, bridge: Bridge, embedder=None
    ) -> "DuckdbEngine":
        data_dir = Path(data_dir)
        conn = await bridge.run(lambda: duckdb.connect(str(data_dir / "duck.db")))
        engine = cls(bridge, conn, schema, FileMirror(data_dir / "files"))
        await bridge.run(engine._create_tables)
        if embedder is not None and any(s.searchable for s in schema.tables):
            from .search import SearchEngine

            engine._search = await SearchEngine.create(bridge, conn, schema, embedder)
        return engine

    @property
    def search(self):
        """The vss+fts SearchEngine, or None when no column is searchable."""
        return self._search

    # ─── DDL (append-only event tables; no primary key, no update) ─────

    def _create_tables(self) -> None:
        self._conn.execute("CREATE SEQUENCE IF NOT EXISTS _sb_row_seq")
        for spec in self._schema.tables:
            cols = ", ".join(f"{_ident(c.name)} {c.sql_type}" for c in spec.columns)
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {_phys(spec.name)} ("
                f"{cols}, "
                f"{_ident(DS)} VARCHAR, {_ident(CREATED_AT)} VARCHAR, "
                f"{_ident(DELETED_DS)} VARCHAR, {_ident(DELETED_AT)} VARCHAR, "
                f"{_ident(_SEQ)} BIGINT)"
            )
        self._conn.execute("CREATE SEQUENCE IF NOT EXISTS _sb_outbox_seq")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS _sb_outbox ("
            "seq BIGINT DEFAULT nextval('_sb_outbox_seq') PRIMARY KEY, "
            "ticket VARCHAR, tbl VARCHAR, col VARCHAR, op VARCHAR, pk VARCHAR, "
            "txt VARCHAR, state VARCHAR)"
        )

    # ─── reconstruction views (latest live version as of a horizon) ────

    def _reconstruction(self, spec: TableSpec, ds_start: str | None, ds_end: str | None) -> str:
        pk = _ident(spec.primary_key)
        cols = ", ".join(_ident(c) for c in spec.all_column_names)
        horizon = ("" if ds_end is None
                   else f"WHERE COALESCE({_ident(DS)}, {_ident(DELETED_DS)}) <= '{ds_end}'")
        start = "" if ds_start is None else f" AND {_ident(DS)} >= '{ds_start}'"
        return (
            f"SELECT {cols} FROM ("
            f"SELECT *, row_number() OVER (PARTITION BY {pk} ORDER BY {_ident(_SEQ)} DESC) AS _rn "
            f"FROM (SELECT * FROM {_phys(spec.name)} {horizon})"
            f") WHERE _rn = 1 AND {_ident(DS)} IS NOT NULL{start}"
        )

    def _install_views(
        self, ds_start: str | None, ds_end: str | None, searches: list | None = None
    ) -> None:
        # searches: list of (table, score_name, tmp_table). Group by table; each
        # table's view LEFT JOINs its searches and exposes _score_<col> (plus a
        # bare _score alias when the whole query has exactly one search).
        searches = searches or []
        by_table: dict[str, list[tuple[str, str]]] = {}
        for table, name, tmp in searches:
            by_table.setdefault(table, []).append((name, tmp))
        single = len(searches) == 1

        for spec in self._schema.tables:
            recon = self._reconstruction(spec, ds_start, ds_end)
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
                body = f"SELECT base.*, {', '.join(scores)} FROM ({recon}) base {' '.join(joins)}"
            else:
                body = recon
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
            # read-only guard via DuckDB's own statement-type detection — robust
            # against `WITH … DELETE` and multi-statement bypasses that a
            # first-token check misses.
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

    # ─── write (append-only: put/del events, files-first) ──────────────

    def _insert_puts(self, spec: TableSpec, records: list[dict]) -> None:
        json_cols = {c.name for c in spec.columns if c.type == "json"}
        cols = [*spec.column_names, DS, CREATED_AT]
        col_sql = ", ".join(_ident(c) for c in cols) + f", {_ident(_SEQ)}"
        placeholders = ", ".join("?" * len(cols)) + ", nextval('_sb_row_seq')"
        sql = f"INSERT INTO {_phys(spec.name)} ({col_sql}) VALUES ({placeholders})"
        payload: list[list[Any]] = []
        for rec in records:
            values = []
            for c in spec.column_names:
                v = rec.get(c)
                if c in json_cols and v is not None:
                    v = json.dumps(v)
                values.append(v)
            values += [rec.get(DS), rec.get(CREATED_AT)]
            payload.append(values)
        self._conn.executemany(sql, payload)

    def _insert_dels(self, spec: TableSpec, keys: list, ds: str, deleted_at) -> None:
        pk = spec.primary_key
        sql = (f"INSERT INTO {_phys(spec.name)} "
               f"({_ident(pk)}, {_ident(DELETED_DS)}, {_ident(DELETED_AT)}, {_ident(_SEQ)}) "
               f"VALUES (?, ?, ?, nextval('_sb_row_seq'))")
        self._conn.executemany(sql, [[k, ds, deleted_at] for k in keys])

    def _enqueue(self, jobs: list[tuple]) -> None:
        """Each job = (ticket, tbl, col, op, pk, txt)."""
        if jobs:
            self._conn.executemany(
                "INSERT INTO _sb_outbox (ticket, tbl, col, op, pk, txt, state) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                [list(j) for j in jobs])

    async def insert(self, table: str, rows: list[dict], ticket: str) -> None:
        spec = self._schema.table(table)
        now, ds = _utc_now(), _today_ds()
        records: list[dict] = []
        for row in rows:
            unknown = set(row) - set(spec.column_names)
            if unknown:
                raise QueryError(f"{table}: unknown column(s) {sorted(unknown)}")
            rec = {c: row.get(c) for c in spec.column_names}
            rec.update({DS: ds, CREATED_AT: now, DELETED_DS: None, DELETED_AT: None})
            records.append(rec)

        jobs = []
        for rec in records:
            for col in spec.searchable:                 # one vector per searchable column
                v = rec.get(col)
                if v is not None and str(v) != "":
                    jobs.append((ticket, table, col, "upsert", str(rec[spec.primary_key]), str(v)))

        await self._bridge.run(lambda: [self._mirror.append(ds, table, r) for r in records])

        def _db() -> None:
            self._insert_puts(spec, records)
            self._enqueue(jobs)

        await self._bridge.run(_db)

    async def tombstone(self, table: str, where: str, params: list[Any], ticket: str) -> int:
        spec = self._schema.table(table)
        stmt = _one_statement(where, "delete where")
        now, ds = _utc_now(), _today_ds()
        pk = spec.primary_key

        def _do() -> int:
            try:
                # evaluate `where` against the CURRENT live state
                self._install_views(None, None, None)
                cur = self._conn.execute(
                    f"SELECT {_ident(pk)} FROM {_ident(table)} WHERE ({stmt})", list(params))
                keys = [r[0] for r in cur.fetchall()]
                for k in keys:
                    self._mirror.append(ds, table, {"_deleted": k, DELETED_AT: now})
                if keys:
                    self._insert_dels(spec, keys, ds, now)
                    self._enqueue([(ticket, table, col, "delete", str(k), None)
                                   for k in keys for col in spec.searchable])
                return len(keys)
            except duckdb.Error as e:
                raise QueryError(f"{table}: bad delete where: {e}") from e

        return await self._bridge.run(_do)

    # ─── outbox (consumer-facing) ──────────────────────────────────────

    async def outbox_fetch_pending(self, limit: int) -> list[tuple]:
        return await self._bridge.run(lambda: self._conn.execute(
            "SELECT seq, tbl, col, op, pk, txt FROM _sb_outbox WHERE state='pending' "
            "ORDER BY seq LIMIT ?", [limit]).fetchall())

    async def outbox_mark_done(self, seq: int) -> None:
        await self._bridge.run(
            lambda: self._conn.execute("UPDATE _sb_outbox SET state='done' WHERE seq=?", [seq]))

    async def outbox_pending_count(self, ticket: str) -> int:
        r = await self._bridge.run(lambda: self._conn.execute(
            "SELECT count(*) FROM _sb_outbox WHERE ticket=? AND state='pending'", [ticket]
        ).fetchone())
        return int(r[0])

    # ─── rebuild (replay the file mirror in ds order) ──────────────────

    async def rebuild(self) -> dict:
        def _do() -> dict:
            tables = rows = tombs = 0
            for spec in self._schema.tables:
                self._conn.execute(f"DELETE FROM {_phys(spec.name)}")
            self._conn.execute("DELETE FROM _sb_outbox")
            if self._search is not None:
                self._search.reset_inline()   # clear derived vec/fts; consumer refills
            for spec in self._schema.tables:
                tables += 1
                for ds, rec in self._mirror.iter_events(spec.name):
                    if "_deleted" in rec:
                        self._insert_dels(spec, [rec["_deleted"]], ds, rec.get(DELETED_AT))
                        self._enqueue([("rebuild", spec.name, col, "delete", str(rec["_deleted"]), None)
                                       for col in spec.searchable])
                        tombs += 1
                    else:
                        self._insert_puts(spec, [rec])
                        for col in spec.searchable:
                            v = rec.get(col)
                            if v is not None and str(v) != "":
                                self._enqueue([("rebuild", spec.name, col, "upsert",
                                                str(rec[spec.primary_key]), str(v))])
                        rows += 1
            return {"tables": tables, "rows": rows, "tombstones": tombs}

        return await self._bridge.run(_do)

    async def close(self) -> None:
        await self._bridge.run(self._conn.close)

    @property
    def schema(self) -> Schema:
        return self._schema
