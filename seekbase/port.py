"""The public port: ``Seekbase`` (async façade) + ``QueryBuilder`` (chain).

Two forms, one surface (DESIGN §9):

- ``await Seekbase.open(data_dir, schema=..., embedder=...)`` — embedded,
  in-process on DuckDB.
- ``await Seekbase.connect(url, ...)`` — remote, talking to a seekbase server
  over HTTP.

Calling code is identical either way: ``table()`` returns the same lazy,
immutable ``QueryBuilder``; only the executor behind the port differs.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ._engine.bridge import Bridge
from ._engine.duck import DuckdbEngine
from ._engine.executor import HttpExecutor, LocalExecutor
from ._engine.plan import Predicate, Request
from ._types import Embedder, EmbedderInvalid, NotSupportedYet, Row
from .schema import parse_schema


class _Unset:
    """Sentinel: distinguishes 'use the connection's as_of' from an explicit
    as_of=None (the server passes the wire as_of, which may be None)."""


_UNSET = _Unset()


class Seekbase:
    """A supabase-style data port — embedded (``open``) or remote (``connect``)."""

    def __init__(self, executor, *, as_of: str | None) -> None:
        self._exec = executor
        self._as_of = as_of
        self._closed = False

    # ─── open: embedded (in-process DuckDB) ────────────────────────────

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

        # tables with searchable columns need an embedder (enforced at open so
        # the failure is early — even though M1 has no vector engine yet).
        needs_embed = any(t.searchable for t in parsed.tables.values())
        if needs_embed and embedder is None:
            raise EmbedderInvalid(
                "schema declares searchable columns but no embedder was provided"
            )

        bridge = Bridge()
        duck = await DuckdbEngine.open(data_dir / "duck.db", parsed, bridge)
        return cls(LocalExecutor(bridge, duck), as_of=as_of)

    # ─── connect: remote (HTTP to a seekbase server) ───────────────────

    @classmethod
    async def connect(
        cls,
        url: str,
        *,
        api_key: str | None = None,
        as_of: str | None = None,
        transport=None,
    ) -> "Seekbase":
        """Talk to a running seekbase server. Same port, HTTP transport. The
        schema and embedder live on the server; the client carries neither."""
        return cls(HttpExecutor(url, api_key=api_key, transport=transport), as_of=as_of)

    # ─── surface ───────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._exec.ready

    def table(self, name: str) -> "QueryBuilder":
        return QueryBuilder(_db=self, _table=name)

    async def sql(self, statement: str) -> list[Row]:
        """Read-only SQL passthrough (SELECT/WITH). Writes must go through the ORM."""
        return await self._dispatch(Request(op="sql", statement=statement))

    async def flush(self) -> None:
        """Drain the outbox so ``search()`` reflects just-written rows.

        No-op until the vector engine lands (M3); kept on the surface so the
        contract — and HTTP semantics (DESIGN §9) — are stable from day one.
        """
        await self._dispatch(Request(op="flush"))

    async def rebuild(self) -> None:
        await self._dispatch(Request(op="rebuild"))

    async def vacuum(self, *, before: str) -> None:
        await self._dispatch(Request(op="vacuum", before=before))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._exec.close()

    async def __aenter__(self) -> "Seekbase":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ─── dispatch (used by QueryBuilder and the server handler) ─────────

    async def _dispatch(self, req: Request, as_of: Any = _UNSET) -> Any:
        if as_of is _UNSET:
            as_of = self._as_of
        return await self._exec.execute(req, as_of)


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
        return await self._db._dispatch(self._to_request(op="count"))

    # ─── compile + execute ─────────────────────────────────────────────

    def _to_request(self, op: str | None = None) -> Request:
        return Request(
            op=op or self._mode,
            table=self._table,
            columns=self._columns,
            predicates=self._predicates,
            orders=self._orders,
            limit=self._limit,
            offset=self._offset,
            rows=self._rows,
        )

    def __await__(self):
        return self._db._dispatch(self._to_request()).__await__()
