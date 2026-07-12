"""ReadService — the read use case.

Rewrite ``search(col, 'text')`` out of the SQL (the ``_search_*`` helpers below),
embed + tokenize each query text via EmbeddingService, run the hybrid (vss+fts)
retrieval per search via ``StoreService.hybrid``, then hand the rewritten SQL +
score results to ``StoreService.run_query`` against the visibility view. Mirrors
``api/query.py`` / docs/api/query.md.

The SQL-rewrite helpers are regex-based, with known edge cases (comments,
``search(col, ?)`` params) noted in docs/works/search.md §3 — a DuckDB-parser
version is a follow-up (DESIGN §10).
"""
from __future__ import annotations

import re

from .._types import QueryError
from ..struct import Schema

_SEARCH_K = 100

_SEARCH_RE = re.compile(
    r"search\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*'((?:[^']|'')*)'\s*\)", re.IGNORECASE)


def _extract_searches(sql: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Replace each ``search(column, 'literal')`` with ``(_score_<column> IS NOT
    NULL)`` and return (rewritten sql, [(column, text, score_col), …]). The
    score column is ``_score_<column>`` (deduped on collision); ``column`` is a
    validated identifier, so the score name is always safe."""
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
        return f'("{name}" IS NOT NULL)'

    return _SEARCH_RE.sub(_repl, sql), specs


def _search_target(schema: Schema, sql: str, col: str) -> str:
    """The single table referenced by ``sql`` that has ``col`` as searchable."""
    hits = [t.name for t in schema.tables
            if col in t.searchable and re.search(rf"\b{re.escape(t.name)}\b", sql)]
    if len(hits) != 1:
        raise QueryError(
            f"search({col}, …) needs exactly one table with a searchable "
            f"{col!r} column in the query")
    return hits[0]


class ReadService:
    def __init__(self, store, embedding, schema) -> None:
        self._store = store
        self._embedding = embedding
        self._schema = schema

    async def query(
        self, sql: str | None, params, ds_start: str | None, ds_end: str | None
    ) -> dict:
        sql = sql or ""
        rewritten, specs = _extract_searches(sql)
        searches: list[tuple[str, str, list[tuple[str, float]]]] | None = None
        if specs:
            if self._embedding is None:
                raise QueryError("search() needs a searchable column + an embedder")
            searches = []
            for col, text, name in specs:
                target = _search_target(self._schema, sql, col)
                qvec = (await self._embedding.embed([text]))[0]
                results = await self._store.hybrid(
                    target, col, qvec, self._embedding.tok(text), _SEARCH_K,
                    ds_start=ds_start, ds_end=ds_end)
                searches.append((target, name, results))
        rows = await self._store.run_query(rewritten, list(params), ds_start, ds_end, searches)
        return {"rows": rows}
