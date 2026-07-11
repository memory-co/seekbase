"""Executors — the seam between the two forms.

``LocalExecutor`` is a thin forwarder: it maps a ``Request``'s op to the matching
service method and returns whatever the service returns (the full response —
rows / ticket dict). It exists so the embedded port can stay transport-agnostic
(``open`` gets a LocalExecutor, ``connect`` gets an HttpExecutor). The HTTP
``api/`` handlers call the same services directly — no op indirection there.

``HttpExecutor`` sends the same ``Request`` to the matching HTTP endpoint.
"""
from __future__ import annotations

from typing import Any

from .._types import QueryError
from ..struct import Ticket
from .bridge import Bridge


class LocalExecutor:
    def __init__(self, bridge: Bridge, services, duck) -> None:
        self._bridge = bridge
        self._svc = services
        self._duck = duck                    # held for lifecycle (close) only

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
        await self._duck.close()             # closes the single connection (vss+fts included)
        self._bridge.close()


class HttpExecutor:
    def __init__(self, base_url: str, *, api_key: str | None = None, transport=None,
                 timeout: float = 30.0) -> None:
        import httpx

        headers = {}
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers,
            transport=transport, timeout=timeout,
        )

    @property
    def ready(self) -> bool:
        return True

    async def execute(self, req) -> Any:
        # ticket ops return a Ticket (reconstructed from the JSON), so the port
        # sees the same type as it does locally; query returns {"rows": …}.
        op = req.op
        if op == "query":
            return await self._post("/v1/query", {
                "sql": req.sql, "params": list(req.params),
                "ds_start": req.ds_start, "ds_end": req.ds_end})
        if op == "insert":
            return Ticket.from_wire(
                await self._post("/v1/insert", {"table": req.table, "rows": list(req.rows)}))
        if op == "delete":
            return Ticket.from_wire(await self._post("/v1/delete", {
                "table": req.table, "where": req.where, "params": list(req.params)}))
        if op == "status":
            return Ticket.from_wire(await self._get(f"/v1/writes/{req.ticket}"))
        if op == "rebuild":
            return Ticket.from_wire(await self._post("/v1/rebuild", {}))
        raise QueryError(f"unknown op {op!r}")

    async def _post(self, path: str, body: dict) -> Any:
        return self._unwrap(await self._client.post(path, json=body))

    async def _get(self, path: str) -> Any:
        return self._unwrap(await self._client.get(path))

    def _unwrap(self, resp) -> Any:
        from .._wire import exception_from
        data = resp.json()
        if resp.status_code >= 400:
            raise exception_from(data.get("error", {}))
        return data

    async def close(self) -> None:
        await self._client.aclose()
