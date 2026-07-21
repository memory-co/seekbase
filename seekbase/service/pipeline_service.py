"""PipelineService — the read use case: an SPL-style pipeline compiler.

A query is ``stage | stage | …``. Each segment is classified by its *leading
token*: a hit in the operator registry → that operator; a miss → the whole
segment is one DuckDB SQL statement. **SQL is first-class and the default** —
"unknown operator" does not exist (docs/works/pipeline-as-anything.md §6).

The pipeline is not executed by seekbase: it is *compiled* onto the pipeline
runtimes (docs/works/pipeline-runtime-optimize.md). Each segment is assigned a
runtime by what it implements (SQL / ``optimize_duck`` → duck;
``optimize_bash`` → bash), contiguous same-runtime segments **fuse** — duck
runs into one ``WITH`` chain, bash runs into one process pipeline — and the
remaining seams are the real runtime switch points, bridged by materializing
``_in`` as JSONL (the ② 切段 path; the ③ inline-bridge/vtab is a future
optimization). A pure-SQL query (zero pipes) bypasses compilation entirely.

Authorization is compile-time: every operator segment's ``caps`` are checked
against the active :class:`Policy` before anything runs — ``sh`` under the
default ``read-only`` policy is refused before the pipeline starts.
"""
from __future__ import annotations

import re

from .._types import QueryError
from ..operator import OperatorCtx, Registry, builtin_operators
from ..operator.base import split_args
from ..operator.policy import Policy

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
    """Parse → authorize → assign runtimes → fuse → hand the plan to the store.
    Replaces the old ReadService (regex ``search()`` rewrite — retired)."""

    def __init__(self, store, embedding, schema, registry: Registry | None = None,
                 policy: Policy | None = None) -> None:
        self._store = store
        self._embedding = embedding
        self._schema = schema
        self._policy = policy or Policy()
        if registry is None:
            registry = Registry()
            for op in builtin_operators():
                registry.register(op)
        self._registry = registry

    @property
    def registry(self) -> Registry:
        return self._registry

    @property
    def policy(self) -> Policy:
        return self._policy

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

        phases = await self._compile(segments, list(params), ds_start, ds_end)
        rows = await self._store.run_plan(phases, ds_start, ds_end, self._policy.exec_timeout)
        return {"rows": rows}

    # ─── classification: leading token → operator | None (= SQL) ────────

    def _classify(self, segment: str):
        m = _LEAD.match(segment)
        return self._registry.resolve(m.group(1)) if m else None

    # ─── compile: authorize → assign runtime per segment → fuse runs ────

    async def _compile(
        self, segments: list[str], user_params: list, ds_start: str | None, ds_end: str | None
    ) -> list[tuple]:
        """Compile into an execution *plan*: an ordered list of fused phases —
        ``("duck", sql, params)`` (one WITH chain per contiguous duck run) and
        ``("bash", [argv, …])`` (one process pipeline per contiguous bash run).
        Adjacent phases exchange ``_in`` as JSONL (the runtime seam)."""
        ctx = OperatorCtx(
            store=self._store, embedding=self._embedding, schema=self._schema,
            ds_start=ds_start, ds_end=ds_end)

        # 1. classify + authorize + assign a runtime to every segment
        staged: list[tuple] = []            # ("duck-op"|"duck-sql"|"bash", payload…)
        for i, seg in enumerate(segments):
            op = self._classify(seg)
            if op is None:
                if ";" in seg:
                    raise QueryError("a pipeline SQL segment must be a single statement")
                staged.append(("duck-sql", seg))
                continue
            self._policy.check(op)                       # ★ compile-time authorization
            if not op.bounded:
                raise QueryError(
                    f"{op.name} is an unbounded source — it cannot enter a bounded "
                    f"query (the duck runtime needs a finite relation); use db.stream()")
            if op.name == "ingest":
                raise QueryError("ingest is a streaming sink; use db.stream()")
            m = _LEAD.match(seg)
            args = op.parse_args(split_args(seg[m.end():]))
            await op.prepare(args, ctx)
            if op.has("optimize_duck"):
                staged.append(("duck-op", op, args))
            elif op.has("optimize_bash"):
                if op.is_source():
                    raise QueryError(f"{op.name} cannot start a bounded query")
                staged.append(("bash", op, args))
            else:  # pragma: no cover — registry guarantees ≥1 cell
                raise QueryError(f"operator {op.name!r} has no runtime implementation")

        # 2. fuse contiguous runs into phases
        phases: list[tuple] = []
        i = 0
        while i < len(staged):
            kind = staged[i][0]
            if kind == "bash":
                argvs = []
                while i < len(staged) and staged[i][0] == "bash":
                    _, op, args = staged[i]
                    argvs.append(op.optimize_bash(args))
                    i += 1
                if not phases:
                    raise QueryError("a bash segment cannot start a bounded query")
                phases.append(("bash", argvs))
            else:
                run = []
                while i < len(staged) and staged[i][0] != "bash":
                    run.append(staged[i])
                    i += 1
                sql, params, user_params = self._fuse_duck_run(
                    run, user_params, from_bridge=bool(phases))
                phases.append(("duck", sql, params))

        if user_params:
            raise QueryError(f"{len(user_params)} unused query params")
        return phases

    def _fuse_duck_run(
        self, run: list[tuple], user_params: list, from_bridge: bool
    ) -> tuple[str, list, list]:
        """Fuse one contiguous duck run into a single WITH chain. If the run
        follows a bash phase, its first ``_in`` is ``_bridge`` (the JSONL the
        store materializes at the seam)."""
        ctes: list[tuple[str, str]] = []
        all_params: list = []
        prev: str | None = "_bridge" if from_bridge else None

        for j, item in enumerate(run):
            name = f"_s{len(ctes)}"
            if item[0] == "duck-op":
                _, op, args = item
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
                _, seg = item
                need = _count_placeholders(seg)
                if need > len(user_params):
                    raise QueryError(
                        f"pipeline segment needs {need} params, "
                        f"only {len(user_params)} left")
                all_params.extend(user_params[:need])
                user_params = user_params[need:]
                body = seg if prev is None else f"WITH _in AS (SELECT * FROM {prev}) {seg}"
            ctes.append((name, body))
            prev = name

        with_sql = ", ".join(f"{n} AS ({b})" for n, b in ctes)
        return f"WITH {with_sql} SELECT * FROM {prev}", all_params, user_params
