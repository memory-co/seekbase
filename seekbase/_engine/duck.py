"""DuckdbEngine — the structured/analytical engine.

DDL (+ engine-managed metadata + the outbox table), the read path (`query`:
read-only SQL over per-request visibility views; `search('…')` rewritten to a
join against vector-search results), the write path (files-first, then DuckDB,
then enqueue vector jobs to the outbox), and `rebuild` (replay the file mirror).
The vector side is drained asynchronously by the executor's consumer.
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

__all__ = ["DuckdbEngine", "extract_search", "search_target"]

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DS_RE = re.compile(r"^\d{8}$")
_SEARCH_RE = re.compile(r"search\(\s*'((?:[^']|'')*)'\s*\)", re.IGNORECASE)


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


def extract_search(sql: str) -> tuple[str, str | None]:
    """If ``sql`` contains ``search('literal')``, return (sql with it replaced by
    ``TRUE``, the literal text). Otherwise (sql, None). Supports one call."""
    m = _SEARCH_RE.search(sql)
    if not m:
        return sql, None
    text = m.group(1).replace("''", "'")
    return _SEARCH_RE.sub("TRUE", sql, count=1), text


def search_target(schema: Schema, sql: str) -> str:
    """The single searchable table referenced by ``sql``."""
    hits = [t.name for t in schema.tables
            if t.searchable and re.search(rf"\b{re.escape(t.name)}\b", sql)]
    if len(hits) != 1:
        raise QueryError("search() needs exactly one searchable table in the query")
    return hits[0]


class DuckdbEngine:
    def __init__(self, bridge: Bridge, conn, schema: Schema, mirror: FileMirror) -> None:
        self._bridge = bridge
        self._conn = conn
        self._schema = schema
        self._mirror = mirror

    @classmethod
    async def open(cls, data_dir: Path, schema: Schema, bridge: Bridge) -> "DuckdbEngine":
        data_dir = Path(data_dir)
        conn = await bridge.run(lambda: duckdb.connect(str(data_dir / "duck.db")))
        engine = cls(bridge, conn, schema, FileMirror(data_dir / "files"))
        await bridge.run(engine._create_tables)
        return engine

    # ─── DDL ───────────────────────────────────────────────────────────

    def _create_tables(self) -> None:
        for spec in self._schema.tables:
            cols = ", ".join(f"{_ident(c.name)} {c.sql_type}" for c in spec.columns)
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {_phys(spec.name)} ("
                f"{cols}, "
                f"{_ident(DS)} VARCHAR, {_ident(CREATED_AT)} VARCHAR, "
                f"{_ident(DELETED_DS)} VARCHAR, {_ident(DELETED_AT)} VARCHAR, "
                f"PRIMARY KEY ({_ident(spec.primary_key)}))"
            )
        self._conn.execute("CREATE SEQUENCE IF NOT EXISTS _sb_outbox_seq")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS _sb_outbox ("
            "seq BIGINT DEFAULT nextval('_sb_outbox_seq') PRIMARY KEY, "
            "ticket VARCHAR, tbl VARCHAR, op VARCHAR, pk VARCHAR, txt VARCHAR, state VARCHAR)"
        )

    # ─── visibility ────────────────────────────────────────────────────

    def _visibility(self, ds_start: str | None, ds_end: str | None) -> str:
        clauses: list[str] = []
        if ds_end is None:
            clauses.append(f"{_ident(DELETED_DS)} IS NULL")
        else:
            clauses.append(f"{_ident(DS)} <= '{ds_end}'")
            clauses.append(
                f"({_ident(DELETED_DS)} IS NULL OR {_ident(DELETED_DS)} > '{ds_end}')"
            )
        if ds_start is not None:
            clauses.append(f"{_ident(DS)} >= '{ds_start}'")
        return " AND ".join(clauses)

    def _install_views(
        self, ds_start: str | None, ds_end: str | None, search_table: str | None = None
    ) -> None:
        vis = self._visibility(ds_start, ds_end)
        for spec in self._schema.tables:
            if spec.name == search_table:
                body = (
                    f"SELECT base.*, s._score FROM {_phys(spec.name)} base "
                    f"JOIN _sb_search s ON CAST(base.{_ident(spec.primary_key)} AS VARCHAR) = s.pk "
                    f"WHERE {vis}"
                )
            else:
                body = f"SELECT * FROM {_phys(spec.name)} WHERE {vis}"
            self._conn.execute(f"CREATE OR REPLACE TEMP VIEW {_ident(spec.name)} AS {body}")

    # ─── read ──────────────────────────────────────────────────────────

    async def query(
        self,
        sql: str,
        params: list[Any],
        ds_start: str | None,
        ds_end: str | None,
        search: tuple[str, list[tuple[str, float]]] | None = None,
    ) -> list[dict]:
        _check_ds("ds_start", ds_start)
        _check_ds("ds_end", ds_end)
        stmt = _one_statement(sql, "query")
        head = stmt.split(None, 1)[0].lower() if stmt else ""
        if head not in {"select", "with"}:
            raise ReadOnlyError("query is read-only (statement must be SELECT/WITH)")

        def _do() -> list[dict]:
            try:
                target = None
                if search is not None:
                    target, results = search
                    self._conn.execute(
                        "CREATE OR REPLACE TEMP TABLE _sb_search (pk VARCHAR, _score DOUBLE)"
                    )
                    if results:
                        self._conn.executemany(
                            "INSERT INTO _sb_search VALUES (?, ?)",
                            [[p, sc] for p, sc in results],
                        )
                self._install_views(ds_start, ds_end, search_table=target)
                cur = self._conn.execute(stmt, list(params))
                names = [d[0] for d in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()]
            except duckdb.Error as e:
                raise QueryError(str(e)) from e

        return await self._bridge.run(_do)

    # ─── write (files-first → DuckDB → outbox) ─────────────────────────

    def _insert_records(self, spec: TableSpec, records: list[dict]) -> None:
        json_cols = {c.name for c in spec.columns if c.type == "json"}
        cols = [*spec.column_names, DS, CREATED_AT, DELETED_DS, DELETED_AT]
        col_sql = ", ".join(_ident(c) for c in cols)
        placeholders = ", ".join("?" * len(cols))
        sql = f"INSERT OR REPLACE INTO {_phys(spec.name)} ({col_sql}) VALUES ({placeholders})"
        payload: list[list[Any]] = []
        for rec in records:
            values = []
            for c in spec.column_names:
                v = rec.get(c)
                if c in json_cols and v is not None:
                    v = json.dumps(v)
                values.append(v)
            values += [rec.get(DS), rec.get(CREATED_AT), rec.get(DELETED_DS), rec.get(DELETED_AT)]
            payload.append(values)
        self._conn.executemany(sql, payload)

    def _enqueue(self, jobs: list[tuple]) -> None:
        if jobs:
            self._conn.executemany(
                "INSERT INTO _sb_outbox (ticket, tbl, op, pk, txt, state) "
                "VALUES (?, ?, ?, ?, ?, 'pending')",
                [list(j) for j in jobs],
            )

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
            text = " ".join(str(rec[c]) for c in spec.searchable if rec.get(c) is not None)
            if spec.searchable and text:
                jobs.append((ticket, table, "upsert", str(rec[spec.primary_key]), text))

        await self._bridge.run(lambda: [self._mirror.append(ds, table, r) for r in records])

        def _db() -> None:
            self._insert_records(spec, records)
            self._enqueue(jobs)

        await self._bridge.run(_db)

    async def tombstone(self, table: str, where: str, params: list[Any], ticket: str) -> int:
        spec = self._schema.table(table)
        stmt = _one_statement(where, "delete where")
        now, ds = _utc_now(), _today_ds()
        pk = spec.primary_key

        def _do() -> int:
            try:
                cur = self._conn.execute(
                    f"SELECT {_ident(pk)} FROM {_phys(table)} "
                    f"WHERE ({stmt}) AND {_ident(DELETED_DS)} IS NULL",
                    list(params),
                )
                keys = [r[0] for r in cur.fetchall()]
                for k in keys:
                    self._mirror.append(ds, table, {"_deleted": k, DELETED_AT: now})
                if keys:
                    self._conn.execute(
                        f"UPDATE {_phys(table)} SET {_ident(DELETED_DS)} = ?, "
                        f"{_ident(DELETED_AT)} = ? WHERE {_ident(pk)} IN "
                        f"({', '.join('?' * len(keys))})",
                        [ds, now, *keys],
                    )
                    if spec.searchable:
                        self._enqueue([(ticket, table, "delete", str(k), None) for k in keys])
                return len(keys)
            except duckdb.Error as e:
                raise QueryError(f"{table}: bad delete where: {e}") from e

        return await self._bridge.run(_do)

    # ─── outbox (consumer-facing) ──────────────────────────────────────

    async def outbox_fetch_pending(self, limit: int) -> list[tuple]:
        return await self._bridge.run(lambda: self._conn.execute(
            "SELECT seq, tbl, op, pk, txt FROM _sb_outbox WHERE state='pending' "
            "ORDER BY seq LIMIT ?", [limit]).fetchall())

    async def outbox_mark_done(self, seq: int) -> None:
        await self._bridge.run(
            lambda: self._conn.execute("UPDATE _sb_outbox SET state='done' WHERE seq=?", [seq]))

    async def outbox_pending_count(self, ticket: str) -> int:
        r = await self._bridge.run(lambda: self._conn.execute(
            "SELECT count(*) FROM _sb_outbox WHERE ticket=? AND state='pending'", [ticket]
        ).fetchone())
        return int(r[0])

    # ─── rebuild (replay the file mirror; re-enqueue vectors) ──────────

    async def rebuild(self) -> dict:
        def _do() -> dict:
            tables = rows = tombs = 0
            for spec in self._schema.tables:
                self._conn.execute(f"DELETE FROM {_phys(spec.name)}")
            self._conn.execute("DELETE FROM _sb_outbox")
            for spec in self._schema.tables:
                tables += 1
                for ds, rec in self._mirror.iter_events(spec.name):
                    if "_deleted" in rec:
                        self._conn.execute(
                            f"UPDATE {_phys(spec.name)} SET {_ident(DELETED_DS)} = ?, "
                            f"{_ident(DELETED_AT)} = ? WHERE {_ident(spec.primary_key)} = ?",
                            [ds, rec.get(DELETED_AT), rec["_deleted"]],
                        )
                        if spec.searchable:
                            self._enqueue([("rebuild", spec.name, "delete", str(rec["_deleted"]), None)])
                        tombs += 1
                    else:
                        self._insert_records(spec, [rec])
                        text = " ".join(
                            str(rec[c]) for c in spec.searchable if rec.get(c) is not None)
                        if spec.searchable and text:
                            self._enqueue([("rebuild", spec.name, "upsert",
                                            str(rec[spec.primary_key]), text)])
                        rows += 1
            return {"tables": tables, "rows": rows, "tombstones": tombs}

        return await self._bridge.run(_do)

    async def close(self) -> None:
        await self._bridge.run(self._conn.close)

    @property
    def schema(self) -> Schema:
        return self._schema
