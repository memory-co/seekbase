"""PipelineService — the read use case: an SPL-style pipeline compiler.

A query is ``stage | stage | …``. Each segment is classified by its *leading
token*: a hit in the operator registry → that operator; a miss → the whole
segment is one DuckDB SQL statement. **SQL is first-class and the default** —
"unknown operator" does not exist (docs/works/pipeline-as-anything.md §6).

The pipeline is not executed by seekbase: it is *compiled* into one DuckDB
``WITH`` chain and handed to the store (docs/works/pipeline-runtime-optimize.md).
Every stage becomes a CTE ``_s<i>``; a SQL segment references the previous
stage as ``_in`` (bound via a nested CTE), so DuckDB's optimizer sees the whole
chain and a pure-SQL query (zero pipes) bypasses compilation entirely.

M1 scope: the duck runtime only. Operators must lower natively
(``optimize_duck``); the bash runtime / materialized ``run_*`` bridges are M2.
"""
from __future__ import annotations

import re

from .._types import QueryError
from ..operator import OperatorCtx, Registry, builtin_operators
from ..operator.base import split_args

__all__ = ["PipelineService", "split_pipeline"]

_LEAD = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)")


def split_pipeline(text: str) -> list[str]:
    """Split on top-level ``|`` — outside string literals, and never on ``||``
    (SQL string concat). Parentheses don't nest pipes; quotes are the only
    escape that matters."""
    segs: list[str] = []
    buf: list[str] = []
    quote: str | None = None            # "'" or '"' while inside a literal
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote:
            buf.append(c)
            if c == quote:
                if i + 1 < n and text[i + 1] == quote:   # doubled quote = escaped
                    buf.append(text[i + 1])
                    i += 2
                    continue
                quote = None
        elif c in ("'", '"'):
            quote = c
            buf.append(c)
        elif c == "|":
            if i + 1 < n and text[i + 1] == "|":         # `||` = SQL concat
                buf.append("||")
                i += 2
                continue
            segs.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    segs.append("".join(buf))
    out = [s.strip() for s in segs]
    if any(not s for s in out):
        raise QueryError("empty pipeline segment")
    return out


def _count_placeholders(sql: str) -> int:
    """``?`` placeholders outside string literals."""
    count, quote, i, n = 0, None, 0, len(sql)
    while i < n:
        c = sql[i]
        if quote:
            if c == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    i += 2
                    continue
                quote = None
        elif c in ("'", '"'):
            quote = c
        elif c == "?":
            count += 1
        i += 1
    return count


class PipelineService:
    """Parse → prepare → lower → hand one SQL to the store. Replaces the old
    ReadService (regex ``search()`` rewrite + stitch — retired)."""

    def __init__(self, store, embedding, schema, registry: Registry | None = None) -> None:
        self._store = store
        self._embedding = embedding
        self._schema = schema
        if registry is None:
            registry = Registry()
            for op in builtin_operators():
                registry.register(op)
        self._registry = registry

    @property
    def registry(self) -> Registry:
        return self._registry

    async def query(
        self, sql: str | None, params, ds_start: str | None, ds_end: str | None
    ) -> dict:
        text = (sql or "").strip()
        if not text:
            raise QueryError("empty query")
        segments = split_pipeline(text)

        # Pure SQL, zero pipes → not a pipeline at all: straight to the store
        # (visibility views + read-only guard), exactly as before.
        if len(segments) == 1 and self._classify(segments[0]) is None:
            rows = await self._store.run_query(segments[0], list(params), ds_start, ds_end)
            return {"rows": rows}

        final_sql, all_params = await self._compile(segments, list(params), ds_start, ds_end)
        rows = await self._store.run_query(final_sql, all_params, ds_start, ds_end)
        return {"rows": rows}

    # ─── classification: leading token → operator | None (= SQL) ────────

    def _classify(self, segment: str):
        m = _LEAD.match(segment)
        return self._registry.resolve(m.group(1)) if m else None

    # ─── lowering: every stage a CTE, `_in` = the previous stage ────────

    async def _compile(
        self, segments: list[str], user_params: list, ds_start: str | None, ds_end: str | None
    ) -> tuple[str, list]:
        ctx = OperatorCtx(
            store=self._store, embedding=self._embedding, schema=self._schema,
            ds_start=ds_start, ds_end=ds_end)
        ctes: list[tuple[str, str]] = []
        all_params: list = []
        prev: str | None = None

        for i, seg in enumerate(segments):
            name = f"_s{i}"
            op = self._classify(seg)
            if op is not None:
                m = _LEAD.match(seg)
                args = op.parse_args(split_args(seg[m.end():]))
                await op.prepare(args, ctx)
                if op.is_source():
                    if prev is not None:
                        raise QueryError(f"{op.name} is a source — it must start the pipeline")
                    body, op_params = op.optimize_duck(args)
                else:
                    if prev is None:
                        raise QueryError(f"{op.name} needs an upstream — it cannot start a pipeline")
                    body, op_params = op.optimize_duck("_in", args)
                    body = f"WITH _in AS (SELECT * FROM {prev}) {body}"
                all_params.extend(op_params)
            else:
                if ";" in seg:
                    raise QueryError("a pipeline SQL segment must be a single statement")
                need = _count_placeholders(seg)
                if need > len(user_params):
                    raise QueryError(
                        f"pipeline segment {i} needs {need} params, "
                        f"only {len(user_params)} left")
                all_params.extend(user_params[:need])
                user_params = user_params[need:]
                body = seg if prev is None else f"WITH _in AS (SELECT * FROM {prev}) {seg}"
            ctes.append((name, body))
            prev = name

        if user_params:
            raise QueryError(f"{len(user_params)} unused query params")
        with_sql = ", ".join(f"{n} AS ({b})" for n, b in ctes)
        return f"WITH {with_sql} SELECT * FROM {prev}", all_params
