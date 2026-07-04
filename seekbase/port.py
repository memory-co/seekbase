"""The public port: ``Seekbase`` (async façade) + ``QueryBuilder`` (chain).

This is the only surface callers touch. Engines (DuckDB now; vector / files /
outbox later) live behind it and are never exposed. The builder is lazy and
immutable — each operator returns a new builder; ``await`` executes.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ._engine._bridge import Bridge
from ._engine.duck import DuckdbEngine, Plan, Predicate
from ._types import (
    Embedder,
    EmbedderInvalid,
    NotSupportedYet,
    QueryError,
    Row,
)
from .schema import parse_schema


class Seekbase:
    """A supabase-style embedded data port. Open one per instance directory."""

    def __init__(self, bridge: Bridge, duck: DuckdbEngine, *, as_of: str | None) -> None:
        self._bridge = bridge
        self._duck = duck
        self._as_of = as_of
        self._closed = False

    @classmethod
    async def open(
        cls,
        data_dir: str | Path,
        *,
        schema: dict,
        embedder: Embedder | None = None,
        as_of: str | None = None,
    ) -> "Seekbase":
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        parsed = parse_schema(schema)

        # tables with searchable columns need an embedder (enforced now so the
        # failure is at open, not on first search — even though M1 has no
        # vector engine yet).
        needs_embed = any(t.searchable for t in parsed.tables.values())
        if needs_embed and embedder is None:
            raise EmbedderInvalid(
                "schema declares searchable columns but no embedder was provided"
            )

        bridge = Bridge()
        duck = await DuckdbEngine.open(data_dir / "duck.db", parsed, bridge, as_of)
        return cls(bridge, duck, as_of=as_of)

    def table(self, name: str) -> "QueryBuilder":
        return QueryBuilder(_db=self, _table=name)

    async def sql(self, statement: str) -> list[Row]:
        """Read-only SQL passthrough (SELECT/WITH). Writes must go through the ORM."""
        return await self._duck.sql(statement)

    async def flush(self) -> None:
        """Drain the outbox so ``search()`` reflects just-written rows.

        No-op until the vector engine lands (M3); kept on the surface so the
        contract — and HTTP semantics (DESIGN §9) — are stable from day one.
        """
        return None

    async def rebuild(self) -> None:
        raise NotSupportedYet("rebuild() lands with the file mirror (M2)")

    async def vacuum(self, *, before: str) -> None:
        raise NotSupportedYet("vacuum() lands with the time machine (M4)")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._duck.close()
        self._bridge.close()

    async def __aenter__(self) -> "Seekbase":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ─── internal execution (called by QueryBuilder) ───────────────────

    async def _run_select(self, qb: "QueryBuilder") -> list[Row]:
        return await self._duck.select(qb._to_plan())

    async def _run_count(self, qb: "QueryBuilder") -> int:
        return await self._duck.count(qb._to_plan())

    async def _run_insert(self, qb: "QueryBuilder") -> None:
        await self._duck.insert(qb._table, list(qb._rows))

    async def _run_delete(self, qb: "QueryBuilder") -> int:
        return await self._duck.tombstone(qb._to_plan())


_FILTER_OPS = ("eq", "neq", "gt", "gte", "lt", "lte", "like", "ilike")


@dataclass(frozen=True)
class QueryBuilder:
    """Lazy, immutable query chain. ``await`` a select/insert/delete to run it."""

    _db: Seekbase
    _table: str
    _mode: str = "select"          # select | insert | delete | search
    _columns: tuple[str, ...] = ()
    _predicates: tuple[Predicate, ...] = ()
    _orders: tuple[tuple[str, bool], ...] = ()
    _limit: int | None = None
    _offset: int | None = None
    _rows: tuple[dict, ...] = ()
    _search_text: str | None = None

    # ─── projection / write intent ─────────────────────────────────────

    def select(self, *columns: str) -> "QueryBuilder":
        return replace(self, _mode="select", _columns=columns)

    def insert(self, rows: dict | list[dict]) -> "QueryBuilder":
        batch = [rows] if isinstance(rows, dict) else list(rows)
        return replace(self, _mode="insert", _rows=tuple(batch))

    def delete(self) -> "QueryBuilder":
        return replace(self, _mode="delete")

    def search(self, text: str, *, mode: str = "semantic") -> "QueryBuilder":
        if mode != "semantic":
            raise NotSupportedYet(f"search mode {mode!r} not supported yet (M3+)")
        return replace(self, _mode="search", _search_text=text)

    # ─── filters ───────────────────────────────────────────────────────

    def _with_pred(self, op: str, column: str, value: Any) -> "QueryBuilder":
        return replace(self, _predicates=(*self._predicates, Predicate(op, column, value)))

    def eq(self, column: str, value: Any) -> "QueryBuilder":
        return self._with_pred("eq", column, value)

    def neq(self, column: str, value: Any) -> "QueryBuilder":
        return self._with_pred("neq", column, value)

    def gt(self, column: str, value: Any) -> "QueryBuilder":
        return self._with_pred("gt", column, value)

    def gte(self, column: str, value: Any) -> "QueryBuilder":
        return self._with_pred("gte", column, value)

    def lt(self, column: str, value: Any) -> "QueryBuilder":
        return self._with_pred("lt", column, value)

    def lte(self, column: str, value: Any) -> "QueryBuilder":
        return self._with_pred("lte", column, value)

    def in_(self, column: str, values: list) -> "QueryBuilder":
        return self._with_pred("in_", column, list(values))

    def like(self, column: str, pattern: str) -> "QueryBuilder":
        return self._with_pred("like", column, pattern)

    def ilike(self, column: str, pattern: str) -> "QueryBuilder":
        return self._with_pred("ilike", column, pattern)

    def is_(self, column: str, value: Any) -> "QueryBuilder":
        return self._with_pred("is_", column, value)

    # ─── ordering / paging ─────────────────────────────────────────────

    def order(self, column: str, *, desc: bool = False) -> "QueryBuilder":
        return replace(self, _orders=(*self._orders, (column, desc)))

    def limit(self, n: int) -> "QueryBuilder":
        return replace(self, _limit=n)

    def offset(self, n: int) -> "QueryBuilder":
        return replace(self, _offset=n)

    # ─── terminal: count (returns awaitable int) ───────────────────────

    async def count(self) -> int:
        return await self._db._run_count(self)

    # ─── compile + execute ─────────────────────────────────────────────

    def _to_plan(self) -> Plan:
        return Plan(
            table=self._table,
            columns=self._columns,
            predicates=self._predicates,
            orders=self._orders,
            limit=self._limit,
            offset=self._offset,
        )

    def __await__(self):
        return self._execute().__await__()

    async def _execute(self):
        if self._mode == "select":
            return await self._db._run_select(self)
        if self._mode == "insert":
            return await self._db._run_insert(self)
        if self._mode == "delete":
            return await self._db._run_delete(self)
        if self._mode == "search":
            raise NotSupportedYet(
                "search() executes with the vector engine (M3); the operator is "
                "accepted now so chains are stable"
            )
        raise QueryError(f"unknown builder mode {self._mode!r}")
