"""Executors — the seam between the two forms.

``LocalExecutor`` runs a Request against the in-process DuckdbEngine + a
VectorEngine, and runs a background consumer that drains the outbox (embed →
LanceDB). ``HttpExecutor`` calls the matching HTTP endpoint on a server.

Writes return a ticket; its state is derived from the outbox (``pending`` until
the ticket's vector jobs are drained, else ``done``). Tables with no searchable
column enqueue nothing → their tickets are ``done`` immediately.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from .._types import NotFound, NotSupportedYet, QueryError
from .bridge import Bridge
from .duck import DuckdbEngine, _DS_RE, extract_search, search_target
from .plan import Request

_SEARCH_K = 100


def _new_ticket() -> str:
    return "wr_" + uuid.uuid4().hex[:16]


class LocalExecutor:
    def __init__(self, bridge: Bridge, duck: DuckdbEngine, vector=None) -> None:
        self._bridge = bridge
        self._duck = duck
        self._vector = vector
        self._tickets: dict[str, dict] = {}
        self._stop = False
        self._consumer: asyncio.Task | None = None

    async def start(self) -> None:
        if self._vector is not None:
            self._consumer = asyncio.create_task(self._consume())

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
            return await self._ticket_result(ticket, "insert", {})
        if op == "delete":
            if not req.where:
                raise QueryError("delete requires a where clause")
            ticket = _new_ticket()
            matched = await self._duck.tombstone(req.table, req.where, list(req.params), ticket)
            return await self._ticket_result(ticket, "delete", {"matched": matched})
        if op == "status":
            meta = self._tickets.get(req.ticket)
            if meta is None:
                raise NotFound(f"unknown ticket {req.ticket!r}")
            return await self._status(meta)
        if op == "rebuild":
            stats = await self._duck.rebuild()
            return await self._ticket_result(_new_ticket(), "rebuild", {"stats": stats})
        if op == "vacuum":
            if not req.before or not _DS_RE.match(req.before):
                raise QueryError("vacuum needs before=YYYYMMDD")
            raise NotSupportedYet("vacuum() lands with the time machine (M4)")
        raise QueryError(f"unknown op {op!r}")

    async def _run_query(self, req: Request) -> list[dict]:
        sql = req.sql or ""
        rewritten, text = extract_search(sql)
        search = None
        if text is not None:
            if self._vector is None:
                raise QueryError("search() needs a searchable table + an embedder")
            target = search_target(self._duck.schema, sql)
            results = await self._vector.search(target, text, _SEARCH_K)
            search, sql = (target, results), rewritten
        return await self._duck.query(sql, list(req.params), req.ds_start, req.ds_end, search=search)

    async def _ticket_result(self, ticket: str, op: str, extra: dict) -> dict:
        meta = {"ticket": ticket, "op": op, "error": None, **extra}
        self._tickets[ticket] = meta
        return await self._status(meta)

    async def _status(self, meta: dict) -> dict:
        pending = await self._duck.outbox_pending_count(meta["ticket"])
        return {**meta, "state": "done" if pending == 0 else "pending"}

    async def _consume(self) -> None:
        while not self._stop:
            try:
                jobs = await self._duck.outbox_fetch_pending(32)
            except Exception:
                await asyncio.sleep(0.05)
                continue
            if not jobs:
                await asyncio.sleep(0.02)
                continue
            for seq, tbl, op, pk, txt in jobs:
                try:
                    if op == "upsert":
                        await self._vector.upsert(tbl, pk, txt)
                    else:
                        await self._vector.delete(tbl, pk)
                    await self._duck.outbox_mark_done(seq)
                except Exception:
                    await asyncio.sleep(0.05)  # transient (e.g. embedder) → retry later

    async def close(self) -> None:
        self._stop = True
        if self._consumer is not None:
            self._consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer
        if self._vector is not None:
            await self._vector.close()
        await self._duck.close()
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
        if op == "vacuum":
            return await self._post("/v1/vacuum", {"before": req.before})
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
