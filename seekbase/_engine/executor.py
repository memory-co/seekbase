"""Executors — the seam between the two forms.

``LocalExecutor`` dispatches a ``Request`` to the in-process service layer
(query / write / admin) and turns write results into tickets. ``HttpExecutor``
sends the same ``Request`` to the matching HTTP endpoint on a server.

Writes are synchronous, so a ticket is ``done`` as soon as the call returns.
"""
from __future__ import annotations

import uuid
from typing import Any

from .._types import NotFound, QueryError
from .bridge import Bridge


def _new_ticket() -> str:
    return "wr_" + uuid.uuid4().hex[:16]


class LocalExecutor:
    def __init__(self, bridge: Bridge, services, duck) -> None:
        self._bridge = bridge
        self._svc = services
        self._duck = duck                    # held for lifecycle (close) only
        self._tickets: dict[str, dict] = {}

    async def start(self) -> None:
        return None                          # nothing async to spin up (writes are synchronous)

    @property
    def ready(self) -> bool:
        return True

    async def execute(self, req) -> Any:
        op = req.op
        if op == "query":
            rows = await self._svc.query.query(req.sql, req.params, req.ds_start, req.ds_end)
            return {"rows": rows}
        if op == "insert":
            await self._svc.write.insert(req.table, list(req.rows))
            return self._ticket_result(_new_ticket(), "insert", {})
        if op == "delete":
            if not req.where:
                raise QueryError("delete requires a where clause")
            matched = await self._svc.write.delete(req.table, req.where, list(req.params))
            return self._ticket_result(_new_ticket(), "delete", {"matched": matched})
        if op == "status":
            meta = self._tickets.get(req.ticket)
            if meta is None:
                raise NotFound(f"unknown ticket {req.ticket!r}")
            return {**meta, "state": "done"}
        if op == "rebuild":
            stats = await self._svc.admin.rebuild()
            return self._ticket_result(_new_ticket(), "rebuild", {"stats": stats})
        raise QueryError(f"unknown op {op!r}")

    def _ticket_result(self, ticket: str, op: str, extra: dict) -> dict:
        meta = {"ticket": ticket, "op": op, "error": None, **extra}
        self._tickets[ticket] = meta
        return {**meta, "state": "done"}     # writes are synchronous → already settled

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

    async def execute(self, req: Request) -> Any:
        op = req.op
        if op == "query":
            return await self._post("/v1/query", {
                "sql": req.sql, "params": list(req.params),
                "ds_start": req.ds_start, "ds_end": req.ds_end})
        if op == "insert":
            return await self._post("/v1/insert", {"table": req.table, "rows": list(req.rows)})
        if op == "delete":
            return await self._post("/v1/delete", {
                "table": req.table, "where": req.where, "params": list(req.params)})
        if op == "status":
            return await self._get(f"/v1/writes/{req.ticket}")
        if op == "rebuild":
            return await self._post("/v1/rebuild", {})
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
