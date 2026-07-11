"""HttpExecutor — the client half of the HTTP API.

The remote counterpart to ``api/*.py`` (the server endpoints): it sends a
``Request`` to the matching endpoint on a running seekbase server and returns
the same types the local path does (``{"rows": …}`` for query, a ``Ticket`` for
writes/status — reconstructed from the JSON), so the client stays
transport-agnostic. Used by ``Seekbase.connect``.
"""
from __future__ import annotations

from typing import Any

from .._types import QueryError
from .._wire import exception_from
from ..struct import Ticket


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
        data = resp.json()
        if resp.status_code >= 400:
            raise exception_from(data.get("error", {}))
        return data

    async def close(self) -> None:
        await self._client.aclose()
