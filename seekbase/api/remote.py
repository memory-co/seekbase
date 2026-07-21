"""HttpExecutor — the client half of the HTTP API.

The remote counterpart to ``api/*.py`` (the server endpoints): it sends a
``Request`` to the matching endpoint on a running seekbase server and returns
the same types the local path does (``{"rows": …}`` for query, a ``Task`` for
writes/status — reconstructed from the JSON), so the client stays
transport-agnostic. Used by ``Seekbase.connect``.
"""
from __future__ import annotations

from typing import Any

from .._types import QueryError
from .._wire import exception_from
from ..struct import Task


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
            body = {"sql": req.sql, "params": list(req.params),
                    "ds_start": req.ds_start, "ds_end": req.ds_end}
            if req.as_task:
                body["as_task"] = True
            return await self._post("/v1/query", body)
        if op == "insert":
            return Task.from_wire(
                await self._post("/v1/insert", {"table": req.table, "rows": list(req.rows)}))
        if op == "delete":
            return Task.from_wire(await self._post("/v1/delete", {
                "table": req.table, "where": req.where, "params": list(req.params)}))
        if op == "status":
            return Task.from_wire(await self._get(f"/v1/tasks/{req.ticket}"))
        if op == "tasks":
            return [Task.from_wire(d) for d in (await self._get("/v1/tasks"))["tasks"]]
        if op == "task_result":
            return await self._get(f"/v1/tasks/{req.ticket}/result")
        if op == "task_cancel":
            return Task.from_wire(await self._post(f"/v1/tasks/{req.ticket}/cancel", {}))
        if op == "rebuild":
            return Task.from_wire(await self._post("/v1/rebuild", {}))
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
