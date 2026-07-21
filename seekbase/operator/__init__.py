"""Pipeline operators — the pluggable operator ABI, its registry, and the
built-ins (docs/works/operator-plugin.md / operator-registry.md).

  base.py      Operator / OperatorCtx / Cap — the subclass contract
  registry.py  Registry — leading-token resolution; SQL is the default
  builtins.py  Search / Scan / Grep — best practices, registered like anyone
"""
from __future__ import annotations

from .base import Cap, Operator, OperatorCtx
from .builtins import Grep, Scan, Search, builtin_operators
from .registry import Registry

__all__ = [
    "Cap", "Operator", "OperatorCtx", "Registry",
    "Search", "Scan", "Grep", "builtin_operators",
]
