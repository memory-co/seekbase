"""QueryService — the read use case.

Rewrite ``search(col, 'text')`` out of the SQL, run the hybrid (vss+fts)
retrieval per search, then hand the rewritten SQL + score results to the engine
to run against the visibility view. Mirrors ``api/query.py`` / docs/api/query.md.
"""
from __future__ import annotations

from typing import Any

from .._engine.rewrite import extract_searches, search_target
from .._types import QueryError

_SEARCH_K = 100


class QueryService:
    def __init__(self, duck, search, schema) -> None:
        self._duck = duck
        self._search = search
        self._schema = schema

    async def query(
        self, sql: str | None, params, ds_start: str | None, ds_end: str | None
    ) -> dict:
        sql = sql or ""
        rewritten, specs = extract_searches(sql)
        searches: list[tuple[str, str, list[tuple[str, float]]]] | None = None
        if specs:
            if self._search is None:
                raise QueryError("search() needs a searchable column + an embedder")
            searches = []
            for col, text, name in specs:
                target = search_target(self._schema, sql, col)
                results = await self._search.hybrid(target, col, text, _SEARCH_K)
                searches.append((target, name, results))
        rows = await self._duck.run_query(rewritten, list(params), ds_start, ds_end, searches)
        return {"rows": rows}
