"""Operator registry — everything is a registered operator; SQL is the default.

A pipeline segment's *leading token* is looked up here: a hit means the segment
is that operator; a miss means the whole segment is a DuckDB SQL statement —
"unknown operator" does not exist (docs/works/operator-registry.md §5).

Names must not collide with SQL leading keywords (that would shadow the SQL
default), and re-registration is an explicit error (no silent override).
"""
from __future__ import annotations

from .._types import QueryError
from .base import Operator

__all__ = ["Registry", "SQL_LEADING_KEYWORDS"]

# SQL statements a read query can legally start with; an operator taking one of
# these names would make that SQL unreachable, so registration refuses them.
SQL_LEADING_KEYWORDS = frozenset({
    "select", "with", "from", "values", "table",
    "show", "describe", "summarize", "pragma", "explain",
})


class Registry:
    def __init__(self) -> None:
        self._ops: dict[str, Operator] = {}

    def register(self, op: Operator) -> None:
        name = (op.name or "").lower()
        if not name:
            raise QueryError("operator has no name")
        if name in SQL_LEADING_KEYWORDS:
            raise QueryError(
                f"operator name {name!r} collides with a SQL leading keyword")
        if name in self._ops:
            raise QueryError(f"operator {name!r} is already registered")
        op.is_source()          # fail fast: must implement at least one duck cell
        self._ops[name] = op

    def resolve(self, token: str) -> Operator | None:
        """The leading-token lookup: hit → the operator; miss → the segment is
        SQL (never an error)."""
        return self._ops.get(token.lower())

    def __iter__(self):
        return iter(self._ops.values())
