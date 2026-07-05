"""Executors — the seam between the two forms (DESIGN §9).

``Seekbase`` and ``QueryBuilder`` are identical in both forms; only the
executor differs:

- ``LocalExecutor``  — embedded: dispatch a Request straight to DuckDB.
- ``HttpExecutor``   — remote: serialize the Request and POST it to a server.

Both implement ``execute(request, as_of)``. The as-of write-guard lives here
(authoritative, server-side) so the same rule holds for both transports.
"""
from __future__ import annotations

from typing import Any

from .._types import NotSupportedYet, QueryError, ReadOnlyError
from .._wire import exception_from, serialize_request
from .bridge import Bridge
from .duck import DuckdbEngine
from .plan import Request

# ops that mutate — forbidden on a time-machine (as_of) connection
_WRITES = {"insert", "delete", "rebuild", "vacuum"}


class LocalExecutor:
    """Embedded executor: runs against an in-process DuckdbEngine."""

    def __init__(self, bridge: Bridge, duck: DuckdbEngine) -> None:
        self._bridge = bridge
        self._duck = duck

    @property
    def ready(self) -> bool:
        return True

    async def execute(self, req: Request, as_of: str | None) -> Any:
        if as_of is not None and req.op in _WRITES:
            raise ReadOnlyError(
                f"cannot {req.op} on a time-machine (as_of) connection"
            )
        op = req.op
        if op == "select":
            return await self._duck.select(req.to_plan(), as_of)
        if op == "count":
            return await self._duck.count(req.to_plan(), as_of)
        if op == "insert":
            return await self._duck.insert(req.table, list(req.rows))
        if op == "delete":
            return await self._duck.tombstone(req.to_plan())
        if op == "sql":
            return await self._duck.sql(req.statement)
        if op == "flush":
            return None  # no outbox yet (M3)
        if op == "search":
            raise NotSupportedYet(
                "search() executes with the vector engine (M3); the operator is "
                "accepted now so chains are stable"
            )
        if op == "rebuild":
            raise NotSupportedYet("rebuild() lands with the file mirror (M2)")
        if op == "vacuum":
            raise NotSupportedYet("vacuum() lands with the time machine (M4)")
        raise QueryError(f"unknown op {op!r}")

    async def close(self) -> None:
        await self._duck.close()
        self._bridge.close()


class HttpExecutor:
    """Remote executor: talks to a seekbase server over HTTP."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        transport=None,       # httpx transport override (e.g. ASGITransport for tests)
        timeout: float = 30.0,
    ) -> None:
        import httpx

        headers = {}
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            transport=transport,
            timeout=timeout,
        )

    @property
    def ready(self) -> bool:
        return True

    async def execute(self, req: Request, as_of: str | None) -> Any:
        resp = await self._client.post(
            "/v1/execute", json=serialize_request(req, as_of)
        )
        data = resp.json()
        if resp.status_code != 200:
            raise exception_from(data.get("error", {}))
        return data["result"]

    async def close(self) -> None:
        await self._client.aclose()
