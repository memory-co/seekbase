"""Operator — the pluggable pipeline operator (docs/works/operator-plugin.md).

One operator = one ``Operator`` subclass. The framework only knows this base:
``search`` and a user-written operator plug into the same ABI. No ``accepts``/
``emits`` — the data format is the *runtime's* medium (duck = relation, bash =
byte stream), fixed by which cell of the execution matrix you implement:

                duck runtime                bash runtime
  optimize_*    optimize_duck → SQL         optimize_bash → argv      (native, 0-cost)
  run_*         run_duck(table) → table     run_bash(stdin, stdout)   (materialized)

All four are optional but at least one must exist. Position (source vs middle)
is derived from the *signature* of ``optimize_duck`` / ``run_duck`` — a source
does not take the upstream relation (docs/works/operator-plugin.md §8).

M1 implementation notes (deviations are deliberate and small):
- Only the duck runtime is compiled today (bash runtime + policy gating is M2);
  ``optimize_bash`` / ``run_bash`` are contract slots.
- ``optimize_duck`` returns ``(sql, params)`` — parameters (e.g. a query
  vector) are data, and carrying them beside the SQL keeps codegen pure.
- ``prepare()`` is an async compile-time hook for argument *derivation* that
  needs services (e.g. search embedding its query text). It is part of argument
  processing, not codegen — ``optimize_*`` itself stays sync and ctx-free.
"""
from __future__ import annotations

import enum
import inspect
import shlex
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from .._types import QueryError

__all__ = ["Cap", "Operator", "OperatorCtx", "parse_tokens"]


class Cap(enum.Enum):
    """What an operator touches outside ``_in`` — the authorization basis
    (docs/works/operator-registry.md §3). Declared honestly, enforced by the
    policy layer (M2); M1 records it on registration."""

    PURE = "pure"
    FS_READ = "fs_read"
    FS_WRITE = "fs_write"
    NET = "net"
    EXEC = "exec"


@dataclass(frozen=True)
class OperatorCtx:
    """The execution context handed to ``prepare`` / ``run_*`` — the only door
    to the outside world. Which members are live depends on the wiring; an
    operator must not reach around it (no ambient authority)."""

    store: Any                       # StoreService (SQL knowledge: visibility, hybrid)
    embedding: Any                   # EmbeddingService or None
    schema: Any                      # parsed Schema
    ds_start: str | None = None      # time-machine window (as-of), pushed into sources
    ds_end: str | None = None


def parse_tokens(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split shell-style tokens into (positional, {--option: value})."""
    pos: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--"):
            if i + 1 >= len(tokens):
                raise QueryError(f"option {t} needs a value")
            opts[t[2:]] = tokens[i + 1]
            i += 2
        else:
            pos.append(t)
            i += 1
    return pos, opts


class Operator:
    """Base class for pipeline operators. Subclass, set ``name`` (+ ``caps``),
    implement at least one execution cell. See module docstring for the matrix.
    """

    name: str = ""
    caps: frozenset = frozenset({Cap.PURE})
    # The one sanctioned declaration (docs/works/operator-plugin.md §3.3): a
    # bash-runtime source must say whether its stream ends. An unbounded source
    # can never enter the duck runtime (a bounded query) — it belongs to
    # ``db.stream`` (docs/works/pipeline-streaming.md).
    bounded: bool = True

    # ── argument parsing (override for validation / per-arg caps) ───────
    def parse_args(self, tokens: list[str]) -> SimpleNamespace:
        pos, opts = parse_tokens(tokens)
        return SimpleNamespace(pos=pos, opts=opts)

    # ── compile-time derivation hook (async; has ctx; mutates args) ─────
    async def prepare(self, args: SimpleNamespace, ctx: OperatorCtx) -> None:
        """Derive argument values that need services (embedding a query text,
        resolving a table). Runs once at compile time, before codegen."""

    # ── native lowering (0-cost; no ctx — codegen only) ────────────────
    # duck source form:  optimize_duck(self, args) -> (sql, params)
    # duck middle form:  optimize_duck(self, prev, args) -> (sql, params)
    #   ``prev`` is the name of the previous stage's relation (a CTE name).
    # bash form:         optimize_bash(self, args) -> argv (list[str])
    #   stdin/stdout are the medium — the signature carries no ``prev``, so
    #   bash-only position defaults to *middle* (override ``is_source`` for a
    #   bash-only source such as ``watch``).
    # Do NOT define them here: presence is detected by override.

    # ── materialized execution (barrier; has ctx) — future surface ─────
    # duck source form:  run_duck(self, args, ctx) -> rows
    # duck middle form:  run_duck(self, in_table, args, ctx) -> rows
    # bash:              run_bash(self, stdin, stdout, args, ctx)

    # ── lifecycle (service-backed operators only) ──────────────────────
    async def start(self, ctx: OperatorCtx) -> None:
        """Bring up resident resources (open once, reuse per call)."""

    async def stop(self) -> None:
        """Tear down resident resources."""

    # ── derived facts (framework-side; no declared fields) ─────────────

    def has(self, method: str) -> bool:
        """Did this subclass implement ``method``? (presence = capability)"""
        return getattr(type(self), method, None) is not None

    def is_source(self) -> bool:
        """Derived from the signature: a source's ``optimize_duck``/``run_duck``
        does not take the upstream relation (operator-plugin §8). A bash-only
        operator defaults to *middle* (stdin is its implicit upstream); a
        bash-only source (e.g. ``watch``) overrides this method — still a
        derivation seam, not a data field."""
        for m, middle_arity in (("optimize_duck", 2), ("run_duck", 3)):
            fn = getattr(type(self), m, None)
            if fn is not None:
                n = len(inspect.signature(fn).parameters) - 1   # drop self
                return n < middle_arity
        if self.has("optimize_bash") or self.has("run_bash"):
            return False
        raise QueryError(f"operator {self.name!r} implements no executable cell")


def split_args(text: str) -> list[str]:
    """Shell-style tokenization of an operator segment's argument text."""
    try:
        return shlex.split(text, posix=True)
    except ValueError as e:
        raise QueryError(f"bad operator arguments: {e}") from e
