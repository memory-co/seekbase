"""The public port: ``Seekbase``.

Two forms, one surface:
- ``await Seekbase.open(data_dir, schema=…, embedder=…)`` — embedded (DuckDB).
- ``await Seekbase.connect(url, …)`` — remote (HTTP to a seekbase server).

Read is one pipeline interface (``query`` — SQL by default, operator
segments via ``|``, with the ds time window); writes are
async (``insert`` / ``delete`` return a ticket, poll via ``write_status`` /
``wait``). See docs/api/.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ._types import Embedder, EmbedderInvalid, QueryError
from .api.remote import HttpExecutor
from .runtime import Bridge
from .schema import parse_schema
from .service import EmbeddingService, FileService, StoreService, build_services
from .struct import Request, Row, Ticket


class LocalExecutor:
    """The local execution seam: map a ``Request``'s op to a service method and
    return what the service returns (``{"rows": …}`` / a ``Ticket``). The remote
    counterpart is ``api/remote.HttpExecutor``; ``Seekbase`` holds one or the
    other so its methods stay transport-agnostic."""

    def __init__(self, services, store, bridge) -> None:
        self._svc = services
        self._store = store              # held for lifecycle (close) only
        self._bridge = bridge

    async def start(self) -> None:
        await self._svc.write.start()    # launch the write worker (drains the queue)

    @property
    def ready(self) -> bool:
        return True

    async def execute(self, req) -> Any:
        op = req.op
        if op == "query":
            return await self._svc.read.query(req.sql, req.params, req.ds_start, req.ds_end)
        if op == "insert":
            return await self._svc.write.insert(req.table, list(req.rows))
        if op == "delete":
            return await self._svc.write.delete(req.table, req.where, list(req.params))
        if op == "status":
            return await self._svc.write.status(req.ticket)
        if op == "rebuild":
            return await self._svc.admin.rebuild()
        raise QueryError(f"unknown op {op!r}")

    async def close(self) -> None:
        await self._svc.write.close()    # stop the write worker (drain + join)
        await self._store.close()        # closes the single DuckDB connection (vss+fts)
        self._bridge.close()


class Seekbase:
    """A supabase-style data port — embedded (``open``) or remote (``connect``)."""

    def __init__(self, executor, services=None) -> None:
        self._exec = executor
        self._services = services     # local use-case services (None when connected remotely)
        self._closed = False

    @property
    def services(self):
        """The in-process service layer (read/write/admin). Present for
        an embedded ``open``ed db; ``None`` for a remote ``connect``. The HTTP
        server (which always wraps an embedded db) calls these directly."""
        return self._services

    # ─── open / connect ────────────────────────────────────────────────

    @classmethod
    async def open(
        cls,
        data_dir: str | Path,
        *,
        schema: list,
        embedder: Embedder | None = None,
        search_backend: str = "vss",
    ) -> "Seekbase":
        """``search_backend`` picks the retrieval engine behind the pipeline's
        ``search`` source (docs/works/search.md §5): ``"vss"`` (default —
        DuckDB vss+fts in-table, single file, constant fds) or ``"lance"``
        (side LanceDB datasets via the DuckDB lance extension — versioned,
        per-write fragments; own the fd account)."""
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        parsed = parse_schema(schema)
        has_searchable = any(t.searchable for t in parsed.tables)
        if has_searchable and embedder is None:
            raise EmbedderInvalid(
                "schema declares searchable columns but no embedder was provided"
            )
        bridge = Bridge()
        embedding = EmbeddingService(embedder) if has_searchable else None
        dim = embedding.dim if embedding is not None else None
        store = await StoreService.open(
            data_dir, parsed, bridge, dim=dim, search_backend=search_backend)
        files = FileService(bridge, data_dir / "files")
        services = build_services(store, embedding, files, parsed, bridge, data_dir / "tickets")
        executor = LocalExecutor(services, store, bridge)
        await executor.start()
        return cls(executor, services)

    @classmethod
    async def connect(
        cls, url: str, *, api_key: str | None = None, transport=None
    ) -> "Seekbase":
        """Talk to a running seekbase server. Same surface, HTTP transport.
        The schema and embedder live on the server; the client carries neither."""
        return cls(HttpExecutor(url, api_key=api_key, transport=transport))

    # ─── read ──────────────────────────────────────────────────────────

    async def query(
        self,
        sql: str,
        *,
        params: list | None = None,
        ds_start: str | None = None,
        ds_end: str | None = None,
    ) -> list[Row]:
        """Read-only pipeline query. Pure SQL runs as-is (zero pipes); a
        ``search <table> 'text' | SELECT … FROM _in`` pipeline compiles into one
        WITH SQL. The ``ds_start``/``ds_end`` time window applies to the whole
        pipeline, search candidates included (see docs/api/query.md)."""
        res = await self._exec.execute(Request(
            op="query", sql=sql, params=tuple(params or ()),
            ds_start=ds_start, ds_end=ds_end,
        ))
        return res["rows"]

    # ─── write (returns a ticket id; poll its Ticket via write_status) ──

    async def insert(self, table: str, rows: dict | list[dict]) -> str:
        batch = [rows] if isinstance(rows, dict) else list(rows)
        t = await self._exec.execute(Request(op="insert", table=table, rows=tuple(batch)))
        return t.id

    async def delete(self, table: str, *, where: str, params: list | None = None) -> str:
        t = await self._exec.execute(Request(
            op="delete", table=table, where=where, params=tuple(params or ()),
        ))
        return t.id

    async def write_status(self, ticket: str) -> Ticket:
        return await self._exec.execute(Request(op="status", ticket=ticket))

    async def wait(self, ticket: str, *, poll: float = 0.05) -> Ticket:
        """Block until the write settles, returning its :class:`Ticket`."""
        while True:
            st = await self.write_status(ticket)
            if st.state != "pending":
                return st
            await asyncio.sleep(poll)

    # ─── admin ─────────────────────────────────────────────────────────

    async def rebuild(self) -> str:
        t = await self._exec.execute(Request(op="rebuild"))
        return t.id

    # ─── lifecycle ─────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._exec.ready

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._exec.close()

    async def __aenter__(self) -> "Seekbase":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
