"""Built-in operators — best practices, not privileged: ``search`` / ``scan`` /
``grep`` register into the same table a user operator would
(docs/works/operator-registry.md §4).

All three lower natively into the duck runtime (``optimize_duck``), so a
pipeline made of them compiles to one WITH-chain SQL — zero bridges
(docs/works/pipeline-runtime-optimize.md §5).
"""
from __future__ import annotations

from types import SimpleNamespace

from .._types import QueryError
from .base import Cap, Operator, OperatorCtx, parse_tokens

__all__ = ["Search", "Scan", "Grep", "builtin_operators"]

_SEARCH_K = 100


class Search(Operator):
    """``search <table> '<text>' [--col <c>] [--k <n>]`` — hybrid (vss + fts,
    RRF) retrieval as a *source*: emits the table's visible columns plus
    ``_score``. The engine behind it is pluggable (docs/works/search.md); this
    wires the duck-vss backend via ``StoreService``.
    """

    name = "search"
    caps = frozenset({Cap.PURE})            # engine-internal; embedder NET is ctx-mediated

    def parse_args(self, tokens):
        pos, opts = parse_tokens(tokens)
        if len(pos) != 2:
            raise QueryError("usage: search <table> '<text>' [--col <c>] [--k <n>]")
        try:
            k = int(opts.get("k", _SEARCH_K))
        except ValueError:
            raise QueryError("search: --k must be an integer") from None
        if k <= 0:
            raise QueryError("search: --k must be positive")
        return SimpleNamespace(table=pos[0], text=pos[1], col=opts.get("col"), k=k)

    async def prepare(self, args, ctx: OperatorCtx) -> None:
        spec = ctx.schema.table(args.table)              # unknown table → SchemaError
        col = args.col
        if col is None:
            if len(spec.searchable) != 1:
                raise QueryError(
                    f"search {args.table}: --col is required "
                    f"(searchable columns: {list(spec.searchable) or 'none'})")
            col = spec.searchable[0]
        elif col not in spec.searchable:
            raise QueryError(f"search {args.table}: column {col!r} is not searchable")
        if ctx.embedding is None:
            raise QueryError("search needs a searchable column + an embedder")
        qvec = (await ctx.embedding.embed([args.text]))[0]
        qtok = ctx.embedding.tok(args.text)
        # Codegen inputs, derived once here (prepare = argument processing);
        # optimize_duck below just hands them over — it stays sync and ctx-free.
        # The engine behind the lowering is pluggable (vss | lance): the store
        # branches per backend, the RRF fusion is shared.
        args._sql, args._params = ctx.store.search_lower(
            args.table, col, qvec, qtok, args.k, ctx.ds_start, ctx.ds_end)

    def optimize_duck(self, args):                       # source: no ``prev``
        return args._sql, args._params


class Scan(Operator):
    """``scan <table>`` — the table's visible rows (time-machine window applies)
    as a source. The pipeline twin of ``FROM <table>``; useful as an explicit
    head for operator chains."""

    name = "scan"
    caps = frozenset({Cap.PURE})

    def parse_args(self, tokens):
        pos, opts = parse_tokens(tokens)
        if len(pos) != 1 or opts:
            raise QueryError("usage: scan <table>")
        return SimpleNamespace(table=pos[0])

    async def prepare(self, args, ctx: OperatorCtx) -> None:
        ctx.schema.table(args.table)                     # unknown table → SchemaError
        args._sql = ctx.store.visible_sql(args.table, ctx.ds_start, ctx.ds_end)

    def optimize_duck(self, args):                       # source
        return args._sql, []


class Grep(Operator):
    """``grep '<pattern>' --field <col>`` — regex row filter over ``_in``.
    Lowered entirely into a WHERE (the canonical "second optimize_* keeps you
    from becoming a switch point" example)."""

    name = "grep"
    caps = frozenset({Cap.PURE})

    def parse_args(self, tokens):
        pos, opts = parse_tokens(tokens)
        if len(pos) != 1 or "field" not in opts:
            raise QueryError("usage: grep '<pattern>' --field <col>")
        return SimpleNamespace(pattern=pos[0], field=opts["field"])

    def optimize_duck(self, prev, args):                 # middle: takes ``prev``
        field = args.field
        if not field.replace("_", "").isalnum():
            raise QueryError(f"grep: illegal field {field!r}")
        return (
            f'SELECT * FROM {prev} WHERE regexp_matches(CAST("{field}" AS VARCHAR), ?)',
            [args.pattern],
        )


class Sh(Operator):
    """``sh '<command>'`` — the escape hatch: an arbitrary shell command as a
    bash-runtime middle. ``_in`` rows stream in as JSONL on stdin; stdout JSONL
    becomes the next ``_in``. ``EXEC`` — denied under the default ``read-only``
    policy, sandboxed when allowed (operator-registry §6)."""

    name = "sh"
    caps = frozenset({Cap.EXEC})

    def parse_args(self, tokens):
        if len(tokens) != 1:
            raise QueryError("usage: sh '<command>'")
        return SimpleNamespace(command=tokens[0])

    def optimize_bash(self, args):                       # bash middle (stdin→stdout)
        return ["/bin/sh", "-c", args.command]


class Jq(Operator):
    """``jq '<script>'`` — wrap the ``jq`` CLI as a bash-runtime middle: rows
    as JSONL through ``jq -c``. Its own reading of JSONL is an implementation
    detail, not a declared format (operator-plugin §4)."""

    name = "jq"
    caps = frozenset({Cap.EXEC})

    def parse_args(self, tokens):
        if len(tokens) != 1:
            raise QueryError("usage: jq '<script>'")
        return SimpleNamespace(script=tokens[0])

    def optimize_bash(self, args):
        return ["jq", "-c", args.script]


class Watch(Operator):
    """``watch '<glob>'`` — the unbounded source (docs/works/pipeline-streaming.md):
    follow files matching the glob, emitting each appended line. Only meaningful
    inside ``db.stream`` — a bounded ``query`` rejects it at compile time
    (an unbounded stream can never enter the duck runtime)."""

    name = "watch"
    caps = frozenset({Cap.FS_READ})
    bounded = False                                      # ★ the one required declaration

    def parse_args(self, tokens):
        pos, opts = parse_tokens(tokens)
        if len(pos) != 1:
            raise QueryError("usage: watch '<glob>' [--poll-ms <n>]")
        try:
            poll_ms = int(opts.get("poll-ms", 200))
        except ValueError:
            raise QueryError("watch: --poll-ms must be an integer") from None
        return SimpleNamespace(glob=pos[0], poll_ms=max(20, poll_ms))

    def is_source(self) -> bool:                         # bash-only source (base defaults to middle)
        return True

    def optimize_bash(self, args):                       # tail -F is the native shape; the stream
        return ["tail", "-F", "-n", "+1", args.glob]     # runtime drives a checkpointed reader instead


class Ingest(Operator):
    """``ingest <table>`` — the streaming sink: read JSONL rows, micro-batch,
    land through the write path (files-first + index), dedupe by primary key
    (at-least-once + idempotent = effectively exactly-once). Writes through the
    port's own write path — not an outside-world capability, hence PURE."""

    name = "ingest"
    caps = frozenset({Cap.PURE})

    def parse_args(self, tokens):
        pos, opts = parse_tokens(tokens)
        if len(pos) != 1:
            raise QueryError("usage: ingest <table> [--batch <n>] [--flush-ms <n>]")
        try:
            batch = int(opts.get("batch", 64))
            flush_ms = int(opts.get("flush-ms", 200))
        except ValueError:
            raise QueryError("ingest: --batch/--flush-ms must be integers") from None
        return SimpleNamespace(table=pos[0], batch=max(1, batch), flush_ms=max(20, flush_ms))

    def is_source(self) -> bool:                         # a sink: executed by the stream
        return False                                     # runtime, not lowered to a cell


def builtin_operators() -> list[Operator]:
    return [Search(), Scan(), Grep(), Sh(), Jq(), Watch(), Ingest()]
