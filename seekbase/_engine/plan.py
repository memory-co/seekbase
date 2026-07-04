"""Transport-neutral query primitives.

``Predicate`` / ``Plan`` describe a compiled query; ``Request`` is the single
unit that flows through an executor — embedded (straight to DuckDB) or over
HTTP (serialized to the server). No engine or transport dependency lives here,
so both sides agree on one shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Predicate:
    op: str
    column: str
    value: Any = None


@dataclass(frozen=True)
class Plan:
    """Compiled-query inputs for the structured engine."""
    table: str
    columns: tuple[str, ...] = ()          # empty -> declared cols + created_at
    predicates: tuple[Predicate, ...] = ()
    orders: tuple[tuple[str, bool], ...] = ()   # (column, desc)
    limit: int | None = None
    offset: int | None = None


@dataclass(frozen=True)
class Request:
    """One port operation. ``op`` in: select | count | insert | delete | search
    | sql | flush | rebuild | vacuum."""
    op: str
    table: str | None = None
    columns: tuple[str, ...] = ()
    predicates: tuple[Predicate, ...] = ()
    orders: tuple[tuple[str, bool], ...] = ()
    limit: int | None = None
    offset: int | None = None
    rows: tuple[dict, ...] = ()        # insert
    statement: str | None = None       # sql
    before: str | None = None          # vacuum
    _extra: dict = field(default_factory=dict)

    def to_plan(self) -> Plan:
        return Plan(
            table=self.table,
            columns=self.columns,
            predicates=self.predicates,
            orders=self.orders,
            limit=self.limit,
            offset=self.offset,
        )
