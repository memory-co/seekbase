"""LocalExecutor — run a Request against the in-process services.

A thin forwarder: map a ``Request``'s op to the matching service method and
return whatever the service returns (the full response — ``{"rows": …}`` or a
``Ticket``). It exists so the embedded client stays transport-agnostic
(``open`` → LocalExecutor, ``connect`` → HttpExecutor in ``api/remote.py``). The
HTTP ``api/`` handlers call the same services directly.
"""
from __future__ import annotations

from typing import Any

from .._types import QueryError


class LocalExecutor:
    def __init__(self, services, store, bridge) -> None:
        self._svc = services
        self._store = store                  # held for lifecycle (close) only
        self._bridge = bridge

    async def start(self) -> None:
        return None                          # nothing async to spin up (writes are synchronous)

    @property
    def ready(self) -> bool:
        return True

    async def execute(self, req) -> Any:
        op = req.op
        if op == "query":
            return await self._svc.query.query(req.sql, req.params, req.ds_start, req.ds_end)
        if op == "insert":
            return await self._svc.write.insert(req.table, list(req.rows))
        if op == "delete":
            return await self._svc.write.delete(req.table, req.where, list(req.params))
        if op == "status":
            return self._svc.tickets.status(req.ticket)
        if op == "rebuild":
            return await self._svc.admin.rebuild()
        raise QueryError(f"unknown op {op!r}")

    async def close(self) -> None:
        await self._store.close()            # closes the single DuckDB connection (vss+fts)
        self._bridge.close()
