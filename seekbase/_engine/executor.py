"""Executors — the seam between the two forms.

``LocalExecutor`` runs a Request against the in-process DuckdbEngine (whose
``search`` is the vss+fts SearchEngine). Writes are **synchronous**: insert
embeds + tokenizes inline and writes the row (vector included) in one shot;
there is no outbox/consumer. ``HttpExecutor`` calls the matching HTTP endpoint.

Writes still return a ticket for API symmetry, but it is ``done`` as soon as the
call returns (the write, including its vectors, is already committed).
"""
from __future__ import annotations

import uuid
from typing import Any

from .._types import NotFound, QueryError
from .bridge import Bridge
from .duck import DuckdbEngine, extract_searches, search_target
from .plan import Request

_SEARCH_K = 100


def _new_ticket() -> str:
    return "wr_" + uuid.uuid4().hex[:16]


class LocalExecutor:
    def __init__(self, bridge: Bridge, duck: DuckdbEngine) -> None:
        self._bridge = bridge
        self._duck = duck
        self._search = duck.search           # vss+fts engine (or None)
        self._tickets: dict[str, dict] = {}

    async def start(self) -> None:
        return None                          # nothing async to spin up (writes are synchronous)

    @property
    def ready(self) -> bool:
        return True

    async def execute(self, req: Request) -> Any:
        op = req.op
        if op == "query":
            return {"rows": await self._run_query(req)}
        if op == "insert":
            ticket = _new_ticket()
            await self._duck.insert(req.table, list(req.rows), ticket)
            return self._ticket_result(ticket, "insert", {})
        if op == "delete":
            if not req.where:
                raise QueryError("delete requires a where clause")
            ticket = _new_ticket()
            matched = await self._duck.tombstone(req.table, req.where, list(req.params), ticket)
            return self._ticket_result(ticket, "delete", {"matched": matched})
        if op == "status":
            meta = self._tickets.get(req.ticket)
            if meta is None:
                raise NotFound(f"unknown ticket {req.ticket!r}")
            return {**meta, "state": "done"}
        if op == "rebuild":
            stats = await self._duck.rebuild()
            return self._ticket_result(_new_ticket(), "rebuild", {"stats": stats})
        raise QueryError(f"unknown op {op!r}")

    async def _run_query(self, req: Request) -> list[dict]:
        sql = req.sql or ""
        rewritten, specs = extract_searches(sql)
        searches = None
        if specs:
            if self._search is None:
                raise QueryError("search() needs a searchable column + an embedder")
            searches = []
            for col, text, name in specs:
                target = search_target(self._duck.schema, sql, col)
                results = await self._search.hybrid(target, col, text, _SEARCH_K)
                searches.append((target, name, results))
            sql = rewritten
        return await self._duck.query(
            sql, list(req.params), req.ds_start, req.ds_end, searches=searches)

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
