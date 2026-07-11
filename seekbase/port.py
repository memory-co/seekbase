"""The public port: ``Seekbase``.

Two forms, one surface:
- ``await Seekbase.open(data_dir, schema=…, embedder=…)`` — embedded (DuckDB).
- ``await Seekbase.connect(url, …)`` — remote (HTTP to a seekbase server).

Read is one SQL interface (``query``, with the ds time window); writes are
async (``insert`` / ``delete`` return a ticket, poll via ``write_status`` /
``wait``). See docs/api/.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ._engine.bridge import Bridge
from ._engine.duck import DuckdbEngine
from ._engine.executor import HttpExecutor, LocalExecutor
from ._engine.files import FileMirror
from ._engine.plan import Request, Row
from ._types import Embedder, EmbedderInvalid
from .schema import parse_schema
from .service import build_services


class Seekbase:
    """A supabase-style data port — embedded (``open``) or remote (``connect``)."""

    def __init__(self, executor, services=None) -> None:
        self._exec = executor
        self._services = services     # local use-case services (None when connected remotely)
        self._closed = False

    @property
    def services(self):
        """The in-process service layer (query/write/admin/tickets). Present for
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
    ) -> "Seekbase":
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        parsed = parse_schema(schema)
        has_searchable = any(t.searchable for t in parsed.tables)
        if has_searchable and embedder is None:
            raise EmbedderInvalid(
                "schema declares searchable columns but no embedder was provided"
            )
        bridge = Bridge()
        files = FileMirror(data_dir / "files")
        duck = await DuckdbEngine.open(data_dir, parsed, bridge, embedder=embedder)
        services = build_services(duck, duck.search, files, bridge, parsed)
        executor = LocalExecutor(bridge, services, duck)
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
        """Read-only SQL. ``search('…')`` semantic filtering (M3) and the
        ``ds_start``/``ds_end`` time window live here (see docs/api/query.md)."""
        res = await self._exec.execute(Request(
            op="query", sql=sql, params=tuple(params or ()),
            ds_start=ds_start, ds_end=ds_end,
        ))
        return res["rows"]

    # ─── write (async: returns a ticket) ───────────────────────────────

    async def insert(self, table: str, rows: dict | list[dict]) -> str:
        batch = [rows] if isinstance(rows, dict) else list(rows)
        res = await self._exec.execute(Request(op="insert", table=table, rows=tuple(batch)))
        return res["ticket"]

    async def delete(self, table: str, *, where: str, params: list | None = None) -> str:
        res = await self._exec.execute(Request(
            op="delete", table=table, where=where, params=tuple(params or ()),
        ))
        return res["ticket"]

    async def write_status(self, ticket: str) -> dict:
        return await self._exec.execute(Request(op="status", ticket=ticket))

    async def wait(self, ticket: str, *, poll: float = 0.05) -> dict:
        """Block until the write settles (``done`` / ``failed``)."""
        while True:
            st = await self.write_status(ticket)
            if st.get("state") != "pending":
                return st
            await asyncio.sleep(poll)

    # ─── admin ─────────────────────────────────────────────────────────

    async def rebuild(self) -> str:
        res = await self._exec.execute(Request(op="rebuild"))
        return res["ticket"]

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
