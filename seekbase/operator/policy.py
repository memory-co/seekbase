"""Policy — capability × policy authorization (docs/works/operator-registry.md §6).

Whether a pipeline may run an operator is decided by its declared ``caps``
against the active policy, at compile time (deny → the pipeline never starts).
Decision order: **deny > allow > mode default** — a denylist wins over
everything; an allowlist (if given) restricts to exactly those names; otherwise
the mode's capability set decides.

Modes (Claude Code / Codex-style):

  read-only (default)  PURE + FS_READ + NET — a data port must not write the
                       filesystem or exec processes by default
  sandboxed            + EXEC, but subprocesses run inside the sandbox bounds
                       (scratch cwd, minimal env, wall-clock timeout — §6.3;
                       network isolation is NOT enforced in-process, be honest)
  trusted              everything, no questions

The ``ask`` (interactive confirm) state from the design is deferred — it needs
a confirmation channel; use allow/deny until then.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .._types import PermissionDenied
from .base import Cap, Operator

__all__ = ["Policy", "SANDBOX_DEFAULT_TIMEOUT"]

SANDBOX_DEFAULT_TIMEOUT = 30.0          # seconds of wall clock per bash phase

_MODE_CAPS: dict[str, frozenset] = {
    "read-only": frozenset({Cap.PURE, Cap.FS_READ, Cap.NET}),
    "sandboxed": frozenset({Cap.PURE, Cap.FS_READ, Cap.NET, Cap.EXEC, Cap.FS_WRITE}),
    "trusted": frozenset(Cap),
}


@dataclass(frozen=True)
class Policy:
    mode: str = "read-only"
    allow: tuple[str, ...] = ()          # allowlist by operator name (empty = no restriction)
    deny: tuple[str, ...] = ()           # denylist by operator name (wins over everything)
    deny_caps: tuple[Cap, ...] = ()      # capability-level deny (survives new operators)
    exec_timeout: float = SANDBOX_DEFAULT_TIMEOUT

    def __post_init__(self):
        if self.mode not in _MODE_CAPS:
            raise PermissionDenied(
                f"unknown policy mode {self.mode!r} (read-only | sandboxed | trusted)")

    def check(self, op: Operator) -> None:
        """Raise :class:`PermissionDenied` if ``op`` may not run under this
        policy. Called once per operator segment at compile time."""
        name = op.name
        if name in self.deny:
            raise PermissionDenied(f"operator {name!r} is denied by policy")
        hit_caps = set(op.caps) & set(self.deny_caps)
        if hit_caps:
            raise PermissionDenied(
                f"operator {name!r} needs denied capability "
                f"{sorted(c.value for c in hit_caps)}")
        if self.allow and name not in self.allow:
            raise PermissionDenied(f"operator {name!r} is not in the policy allowlist")
        missing = set(op.caps) - _MODE_CAPS[self.mode]
        if missing:
            raise PermissionDenied(
                f"operator {name!r} needs {sorted(c.value for c in missing)} — "
                f"not allowed in {self.mode!r} mode (escalate to 'sandboxed'/'trusted')")
