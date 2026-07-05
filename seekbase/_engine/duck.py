"""DuckdbEngine — the structured/analytical engine.

M1-new scope: DDL from the declared schema (with engine-managed ds/created_at/
deleted_ds/deleted_at columns), the read path (`query`: read-only SQL over
per-request visibility views that hide tombstones and apply the ds time window),
and the write path (`insert` / tombstone-`delete`, materialized synchronously —
the async ticket wrapper lives in the executor). The file mirror (M2) and vector
side (M3) slot in around this.

Physical tables are named ``_sb_<table>``; each query creates a same-named TEMP
VIEW (``<table>``) that embeds the visibility predicate, so the caller's SQL
just says ``FROM cards``.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from .._types import QueryError, ReadOnlyError
from ..schema import (
    CREATED_AT,
    DELETED_AT,
    DELETED_DS,
    DS,
    Schema,
    TableSpec,
)
from .bridge import Bridge

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
    def __init__(self, bridge: Bridge, conn, schema: Schema) -> None:
        self._bridge = bridge
        self._conn = conn
        self._schema = schema

    @classmethod
    async def open(cls, path: Path, schema: Schema, bridge: Bridge) -> "DuckdbEngine":
        conn = await bridge.run(lambda: duckdb.connect(str(path)))
        engine = cls(bridge, conn, schema)
        await bridge.run(engine._create_tables)
        return engine

    # ─── DDL ───────────────────────────────────────────────────────────

    def _create_tables(self) -> None:
        for spec in self._schema.tables:
            cols = ", ".join(f"{_ident(c.name)} {c.sql_type}" for c in spec.columns)
            ddl = (
                f"CREATE TABLE IF NOT EXISTS {_phys(spec.name)} ("
                f"{cols}, "
                f"{_ident(DS)} VARCHAR, {_ident(CREATED_AT)} VARCHAR, "
                f"{_ident(DELETED_DS)} VARCHAR, {_ident(DELETED_AT)} VARCHAR, "
                f"PRIMARY KEY ({_ident(spec.primary_key)}))"
            )
            self._conn.execute(ddl)

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
        self,
        sql: str,
        params: list[Any],
        ds_start: str | None,
        ds_end: str | None,
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

    # ─── write ─────────────────────────────────────────────────────────

    async def insert(self, table: str, rows: list[dict]) -> None:
        spec = self._schema.table(table)
        json_cols = {c.name for c in spec.columns if c.type == "json"}
        now, ds = _utc_now(), _today_ds()
        cols = [*spec.column_names, DS, CREATED_AT, DELETED_DS, DELETED_AT]
        col_sql = ", ".join(_ident(c) for c in cols)
        placeholders = ", ".join("?" * len(cols))
        sql = f"INSERT OR REPLACE INTO {_phys(table)} ({col_sql}) VALUES ({placeholders})"

        payload: list[list[Any]] = []
        for row in rows:
            unknown = set(row) - set(spec.column_names)
            if unknown:
                raise QueryError(f"{table}: unknown column(s) {sorted(unknown)}")
            values: list[Any] = []
            for c in spec.column_names:
                v = row.get(c)
                if c in json_cols and v is not None:
                    v = json.dumps(v)
                values.append(v)
            values += [ds, now, None, None]
            payload.append(values)

        await self._bridge.run(lambda: self._conn.executemany(sql, payload))

    async def tombstone(self, table: str, where: str, params: list[Any]) -> int:
        spec = self._schema.table(table)
        stmt = _one_statement(where, "delete where")
        now, ds = _utc_now(), _today_ds()
        sql = (
            f"UPDATE {_phys(table)} SET {_ident(DELETED_DS)} = ?, {_ident(DELETED_AT)} = ? "
            f"WHERE ({stmt}) AND {_ident(DELETED_DS)} IS NULL"
        )

        def _do() -> int:
            cur = self._conn.execute(sql, [ds, now, *params])
            r = cur.fetchone()
            return int(r[0]) if r else 0

        try:
            return await self._bridge.run(_do)
        except duckdb.Error as e:  # bad column / SQL in where
            raise QueryError(f"{table}: bad delete where: {e}") from e

    async def close(self) -> None:
        await self._bridge.run(self._conn.close)

    # a couple of internals the executor may need
    @property
    def schema(self) -> Schema:
        return self._schema
