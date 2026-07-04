"""DuckdbEngine — the structured/analytical engine (DESIGN §6).

M1 scope: DDL from the declared schema, the ORM read/write compilation
(select / insert / tombstone-delete / count), read-only SQL passthrough, and
the as-of visibility rewrite (partial time machine — DESIGN §7). Vector,
outbox and file mirror slot in around this in later milestones.

insert-only is enforced here: there is no UPDATE path except the single
engine-managed tombstone write to ``deleted_at``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from .._types import QueryError, ReadOnlyError
from ..schema import CREATED_AT, DELETED_AT, Schema, TableSpec
from ._bridge import Bridge

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# operator -> SQL template ("?" marks a bound value)
_BINARY_OPS = {
    "eq": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
    "like": "LIKE", "ilike": "ILIKE",
}


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise QueryError(f"illegal identifier {name!r}")
    return f'"{name}"'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Predicate:
    op: str
    column: str
    value: Any = None


@dataclass(frozen=True)
class Plan:
    """Compiled-query inputs, built by the port from a QueryBuilder."""
    table: str
    columns: tuple[str, ...] = ()          # empty -> declared cols + created_at
    predicates: tuple[Predicate, ...] = ()
    orders: tuple[tuple[str, bool], ...] = ()   # (column, desc)
    limit: int | None = None
    offset: int | None = None


class DuckdbEngine:
    def __init__(self, bridge: Bridge, conn, schema: Schema, as_of: str | None) -> None:
        self._bridge = bridge
        self._conn = conn
        self._schema = schema
        self._as_of = as_of

    @classmethod
    async def open(
        cls, path: Path, schema: Schema, bridge: Bridge, as_of: str | None
    ) -> "DuckdbEngine":
        conn = await bridge.run(lambda: duckdb.connect(str(path)))
        engine = cls(bridge, conn, schema, as_of)
        await bridge.run(engine._create_tables)
        return engine

    # ─── DDL ───────────────────────────────────────────────────────────

    def _create_tables(self) -> None:
        for spec in self._schema.tables.values():
            cols = ", ".join(
                f"{_ident(c.name)} {c.sql_type}" for c in spec.columns.values()
            )
            ddl = (
                f"CREATE TABLE IF NOT EXISTS {_ident(spec.name)} ("
                f"{cols}, "
                f"{_ident(CREATED_AT)} VARCHAR, "
                f"{_ident(DELETED_AT)} VARCHAR, "
                f"PRIMARY KEY ({_ident(spec.primary_key)}))"
            )
            self._conn.execute(ddl)

    # ─── visibility (as-of / tombstone) ────────────────────────────────

    def _visibility(self) -> tuple[str, list[Any]]:
        """WHERE fragment restricting to visible rows: current-state hides
        tombstones; as-of rewinds the world to T (DESIGN §7)."""
        if self._as_of is None:
            return f"{_ident(DELETED_AT)} IS NULL", []
        return (
            f"{_ident(CREATED_AT)} <= ? AND "
            f"({_ident(DELETED_AT)} IS NULL OR {_ident(DELETED_AT)} > ?)",
            [self._as_of, self._as_of],
        )

    def _where(self, spec: TableSpec, preds: tuple[Predicate, ...]) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for p in preds:
            if not spec.is_column(p.column):
                raise QueryError(f"{spec.name}: unknown column {p.column!r}")
            col = _ident(p.column)
            if p.op in _BINARY_OPS:
                clauses.append(f"{col} {_BINARY_OPS[p.op]} ?")
                params.append(p.value)
            elif p.op == "in_":
                vals = list(p.value)
                if not vals:
                    clauses.append("FALSE")  # IN () matches nothing
                else:
                    clauses.append(f"{col} IN ({', '.join('?' * len(vals))})")
                    params.extend(vals)
            elif p.op == "is_":
                if p.value is None:
                    clauses.append(f"{col} IS NULL")
                else:
                    clauses.append(f"{col} IS ?")
                    params.append(p.value)
            else:
                raise QueryError(f"unsupported operator {p.op!r}")
        vis_sql, vis_params = self._visibility()
        clauses.append(vis_sql)
        params.extend(vis_params)
        return " AND ".join(clauses), params

    # ─── read ──────────────────────────────────────────────────────────

    def _select_columns(self, spec: TableSpec, plan: Plan) -> str:
        if plan.columns:
            for c in plan.columns:
                if not spec.is_column(c):
                    raise QueryError(f"{spec.name}: unknown column {c!r}")
            names = list(plan.columns)
        else:
            names = [*spec.column_names, CREATED_AT]
        return ", ".join(_ident(c) for c in names)

    async def select(self, plan: Plan) -> list[dict]:
        spec = self._schema.table(plan.table)
        cols = self._select_columns(spec, plan)
        where, params = self._where(spec, plan.predicates)
        sql = f"SELECT {cols} FROM {_ident(spec.name)} WHERE {where}"
        if plan.orders:
            parts = []
            for col, desc in plan.orders:
                if not spec.is_column(col):
                    raise QueryError(f"{spec.name}: unknown order column {col!r}")
                parts.append(f"{_ident(col)} {'DESC' if desc else 'ASC'}")
            sql += " ORDER BY " + ", ".join(parts)
        if plan.limit is not None:
            sql += f" LIMIT {int(plan.limit)}"
        if plan.offset is not None:
            sql += f" OFFSET {int(plan.offset)}"
        return await self._bridge.run(lambda: self._fetch(sql, params))

    async def count(self, plan: Plan) -> int:
        spec = self._schema.table(plan.table)
        where, params = self._where(spec, plan.predicates)
        sql = f"SELECT count(*) FROM {_ident(spec.name)} WHERE {where}"
        rows = await self._bridge.run(lambda: self._conn.execute(sql, params).fetchone())
        return int(rows[0])

    def _fetch(self, sql: str, params: list[Any]) -> list[dict]:
        cur = self._conn.execute(sql, params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    # ─── write (insert-only) ───────────────────────────────────────────

    async def insert(self, table: str, rows: list[dict]) -> None:
        if self._as_of is not None:
            raise ReadOnlyError("cannot write on a time-machine (as_of) connection")
        spec = self._schema.table(table)
        now = _utc_now()
        cols = [*spec.column_names, CREATED_AT, DELETED_AT]
        col_sql = ", ".join(_ident(c) for c in cols)
        placeholders = ", ".join("?" * len(cols))
        sql = f"INSERT INTO {_ident(spec.name)} ({col_sql}) VALUES ({placeholders})"

        payload: list[list[Any]] = []
        for row in rows:
            unknown = set(row) - set(spec.column_names)
            if unknown:
                raise QueryError(f"{table}: unknown column(s) in insert {sorted(unknown)}")
            values = [row.get(c) for c in spec.column_names]
            values.append(row.get(CREATED_AT, now))  # allow caller-supplied created_at
            values.append(None)                       # deleted_at
            payload.append(values)

        def _do() -> None:
            self._conn.executemany(sql, payload)

        await self._bridge.run(_do)

    async def tombstone(self, plan: Plan) -> int:
        """delete() == mark deleted_at. Returns rows tombstoned."""
        if self._as_of is not None:
            raise ReadOnlyError("cannot write on a time-machine (as_of) connection")
        spec = self._schema.table(plan.table)
        where, params = self._where(spec, plan.predicates)
        now = _utc_now()
        sql = f"UPDATE {_ident(spec.name)} SET {_ident(DELETED_AT)} = ? WHERE {where}"

        def _do() -> int:
            cur = self._conn.execute(sql, [now, *params])
            return cur.fetchone()[0] if cur.description else 0

        return await self._bridge.run(_do)

    # ─── read-only SQL passthrough ─────────────────────────────────────

    async def sql(self, statement: str) -> list[dict]:
        head = statement.lstrip().split(None, 1)[0].lower() if statement.strip() else ""
        if head not in {"select", "with"}:
            raise ReadOnlyError(
                "db.sql() is read-only; use the ORM to write (statement must be SELECT/WITH)"
            )
        return await self._bridge.run(lambda: self._fetch(statement, []))

    async def close(self) -> None:
        await self._bridge.run(self._conn.close)
