"""Executors — the seam between the two forms.

``Seekbase`` builds a ``Request`` and hands it to an executor:
- ``LocalExecutor``  — embedded: run it against the in-process DuckdbEngine.
- ``HttpExecutor``   — remote: call the matching HTTP endpoint on a server.

Writes are async at the API level (return a ticket, poll status). In this
milestone materialization is synchronous, so a submitted ticket is already
``done``; the ticket registry lives on the LocalExecutor (the server holds one).
"""
from __future__ import annotations

import uuid
from typing import Any

from .._types import NotFound, NotSupportedYet, QueryError
from .bridge import Bridge
from .duck import DuckdbEngine, _DS_RE
from .plan import Request


def _new_ticket() -> str:
    return "wr_" + uuid.uuid4().hex[:16]


class LocalExecutor:
    """Embedded executor: runs against an in-process DuckdbEngine."""

    def __init__(self, bridge: Bridge, duck: DuckdbEngine) -> None:
        self._bridge = bridge
        self._duck = duck
        self._tickets: dict[str, dict] = {}

    @property
    def ready(self) -> bool:
        return True

    async def execute(self, req: Request) -> Any:
        op = req.op
        if op == "query":
            return {"rows": await self._duck.query(
                req.sql or "", list(req.params), req.ds_start, req.ds_end
            )}
        if op == "insert":
            await self._duck.insert(req.table, list(req.rows))
            return self._record("insert", {})
        if op == "delete":
            if not req.where:
                raise QueryError("delete requires a where clause")
            matched = await self._duck.tombstone(req.table, req.where, list(req.params))
            return self._record("delete", {"matched": matched})
        if op == "status":
            st = self._tickets.get(req.ticket)
            if st is None:
                raise NotFound(f"unknown ticket {req.ticket!r}")
            return st
        if op == "rebuild":
            raise NotSupportedYet("rebuild() lands with the file mirror (M2)")
        if op == "vacuum":
            if not req.before or not _DS_RE.match(req.before):
                raise QueryError("vacuum needs before=YYYYMMDD")
            raise NotSupportedYet("vacuum() lands with the time machine (M4)")
        raise QueryError(f"unknown op {op!r}")

    def _record(self, op: str, extra: dict) -> dict:
        ticket = _new_ticket()
        st = {"ticket": ticket, "op": op, "state": "done", "error": None, **extra}
        self._tickets[ticket] = st
        return st

    async def close(self) -> None:
        await self._duck.close()
        self._bridge.close()


class HttpExecutor:
    """Remote executor: talks to a seekbase server over HTTP."""

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
            body = {"sql": req.sql, "params": list(req.params),
                    "ds_start": req.ds_start, "ds_end": req.ds_end}
            return await self._post("/v1/query", body)
        if op == "insert":
            return await self._post("/v1/insert", {"table": req.table, "rows": list(req.rows)})
        if op == "delete":
            return await self._post(
                "/v1/delete",
                {"table": req.table, "where": req.where, "params": list(req.params)},
            )
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
