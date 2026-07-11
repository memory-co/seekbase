"""SQL rewrite for ``search()`` — pure functions, no engine state.

Turn each ``search(col, 'text')`` into a boolean placeholder that references a
score column, and resolve which table a search targets. The retrieval itself is
done by the QueryService (embed + hybrid); this module only rewrites SQL.

Regex-based, with known edge cases (comments, ``search(col, ?)`` params) noted
in docs/works/search.md §3 — a DuckDB-parser version is a follow-up (DESIGN §10).
"""
from __future__ import annotations

import re

from .._types import QueryError
from ..schema import Schema

_SEARCH_RE = re.compile(
    r"search\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*'((?:[^']|'')*)'\s*\)", re.IGNORECASE)


def extract_searches(sql: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Replace each ``search(column, 'literal')`` with ``(_score_<column> IS NOT
    NULL)`` and return (rewritten sql, [(column, text, score_col), …]). The
    score column is ``_score_<column>`` (deduped on collision). ``column`` is a
    validated identifier, so the score name is always a safe identifier."""
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


def search_target(schema: Schema, sql: str, col: str) -> str:
    """The single table referenced by ``sql`` that has ``col`` as searchable."""
    hits = [t.name for t in schema.tables
            if col in t.searchable and re.search(rf"\b{re.escape(t.name)}\b", sql)]
    if len(hits) != 1:
        raise QueryError(
            f"search({col}, …) needs exactly one table with a searchable "
            f"{col!r} column in the query")
    return hits[0]
