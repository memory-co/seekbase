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
        args._sql = ctx.store.search_cte(args.table, col, args.k, ctx.ds_start, ctx.ds_end)
        args._params = [qvec, qtok, qtok]

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


def builtin_operators() -> list[Operator]:
    return [Search(), Scan(), Grep()]
