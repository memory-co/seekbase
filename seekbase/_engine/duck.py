"""DuckdbEngine — the structured/analytical engine.

Scope: DDL from the declared schema (with engine-managed ds/created_at/
deleted_ds/deleted_at columns), the read path (`query`: read-only SQL over
per-request visibility views that hide tombstones and apply the ds time
window), the write path (`insert` / tombstone-`delete`), and `rebuild` (replay
the file mirror). Writes go files-first (canonical) then DuckDB (derived). The
vector side (M3) slots in around this.

Physical tables are named ``_sb_<table>``; each query creates a same-named TEMP
VIEW (``<table>``) that embeds the visibility predicate.
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

__all__ = ["DuckdbEngine"]

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DS_RE = re.compile(r"^\d{8}$")


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

    def _install_views(self, ds_start: str | None, ds_end: str | None) -> None:
        vis = self._visibility(ds_start, ds_end)
        for spec in self._schema.tables:
            self._conn.execute(
                f"CREATE OR REPLACE TEMP VIEW {_ident(spec.name)} AS "
                f"SELECT * FROM {_phys(spec.name)} WHERE {vis}"
            )

    # ─── read ──────────────────────────────────────────────────────────

    async def query(
        self, sql: str, params: list[Any], ds_start: str | None, ds_end: str | None
    ) -> list[dict]:
        _check_ds("ds_start", ds_start)
        _check_ds("ds_end", ds_end)
        stmt = _one_statement(sql, "query")
        head = stmt.split(None, 1)[0].lower() if stmt else ""
        if head not in {"select", "with"}:
            raise ReadOnlyError("query is read-only (statement must be SELECT/WITH)")

        def _do() -> list[dict]:
            try:
                self._install_views(ds_start, ds_end)
                cur = self._conn.execute(stmt, list(params))
                names = [d[0] for d in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()]
            except duckdb.Error as e:
                raise QueryError(str(e)) from e

        return await self._bridge.run(_do)

    # ─── write (files-first, then DuckDB) ──────────────────────────────

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

    async def insert(self, table: str, rows: list[dict]) -> None:
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

        # ① files first (canonical), ② DuckDB (derived)
        def _files() -> None:
            for rec in records:
                self._mirror.append(ds, table, rec)

        await self._bridge.run(_files)
        await self._bridge.run(lambda: self._insert_records(spec, records))

    async def tombstone(self, table: str, where: str, params: list[Any]) -> int:
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
                # ① files first: a tombstone event in the delete-day partition
                for k in keys:
                    self._mirror.append(ds, table, {"_deleted": k, DELETED_AT: now})
                # ② DuckDB derived row
                if keys:
                    self._conn.execute(
                        f"UPDATE {_phys(table)} SET {_ident(DELETED_DS)} = ?, "
                        f"{_ident(DELETED_AT)} = ? WHERE {_ident(pk)} IN "
                        f"({', '.join('?' * len(keys))})",
                        [ds, now, *keys],
                    )
                return len(keys)
            except duckdb.Error as e:
                raise QueryError(f"{table}: bad delete where: {e}") from e

        return await self._bridge.run(_do)

    # ─── rebuild (replay the file mirror) ──────────────────────────────

    async def rebuild(self) -> dict:
        def _do() -> dict:
            tables = rows = tombs = 0
            for spec in self._schema.tables:
                self._conn.execute(f"DELETE FROM {_phys(spec.name)}")
            for spec in self._schema.tables:
                tables += 1
                for ds, rec in self._mirror.iter_events(spec.name):
                    if "_deleted" in rec:
                        self._conn.execute(
                            f"UPDATE {_phys(spec.name)} SET {_ident(DELETED_DS)} = ?, "
                            f"{_ident(DELETED_AT)} = ? WHERE {_ident(spec.primary_key)} = ?",
                            [ds, rec.get(DELETED_AT), rec["_deleted"]],
                        )
                        tombs += 1
                    else:
                        self._insert_records(spec, [rec])
                        rows += 1
            return {"tables": tables, "rows": rows, "tombstones": tombs}

        return await self._bridge.run(_do)

    async def close(self) -> None:
        await self._bridge.run(self._conn.close)

    @property
    def schema(self) -> Schema:
        return self._schema
