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
from .operator.policy import Policy
from .runtime import Bridge
from .schema import parse_schema
from .service import EmbeddingService, FileService, StoreService, build_services
from .struct import Request, Row, Task, Ticket


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
        await self._svc.task.gc()        # lazy retention: old task logs + result files

    @property
    def ready(self) -> bool:
        return True

    async def execute(self, req) -> Any:
        op = req.op
        if op == "query":
            if req.as_task:
                return await self._svc.task.submit(
                    "query", lambda: self._svc.read.query(
                        req.sql, req.params, req.ds_start, req.ds_end), query=req.sql)
            return await self._svc.read.query(req.sql, req.params, req.ds_start, req.ds_end)
        if op == "insert":
            return await self._svc.write.insert(req.table, list(req.rows))
        if op == "delete":
            return await self._svc.write.delete(req.table, req.where, list(req.params))
        if op == "status":
            return await self._svc.task.status(req.ticket)
        if op == "tasks":
            return await self._svc.task.list(req.limit)
        if op == "task_result":
            return {"rows": await self._svc.task.result(req.ticket)}
        if op == "task_cancel":
            return await self._svc.task.cancel(req.ticket)
        if op == "rebuild":
            return await self._svc.admin.rebuild()
        raise QueryError(f"unknown op {op!r}")

    async def close(self) -> None:
        await self._svc.stream.close()   # stop resident streams (drain + final checkpoint)
        await self._svc.task.close()     # cancel live background tasks
        await self._svc.write.close()    # stop the write worker (drain + join)
        await self._store.close()        # closes the single DuckDB connection
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
        policy: Policy | None = None,
        operators: list | None = None,
    ) -> "Seekbase":
        """``search_backend`` picks the retrieval engine behind the pipeline's
        ``search`` source (docs/works/search.md §5): ``"vss"`` (default —
        DuckDB vss+fts in-table, single file, constant fds) or ``"lance"``
        (side LanceDB datasets via the DuckDB lance extension — versioned,
        per-write fragments; own the fd account).

        ``policy`` bounds what pipeline operators may do (capability × policy,
        default ``read-only`` — ``sh``/``jq`` refused until you escalate to
        ``Policy(mode="sandboxed")``). ``operators`` registers custom
        :class:`~seekbase.Operator` subclasses beside the built-ins."""
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
        services = build_services(store, embedding, files, parsed, bridge,
                                  data_dir / "tasks", policy=policy, operators=operators)
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
        as_task: bool = False,
    ) -> list[Row] | str:
        """Read-only pipeline query. Pure SQL runs as-is (zero pipes); a
        ``search <table> 'text' | SELECT … FROM _in`` pipeline compiles into one
        WITH SQL. The ``ds_start``/``ds_end`` time window applies to the whole
        pipeline, search candidates included (see docs/api/query.md).

        ``as_task=True`` runs the query in the background and returns a task id
        immediately (docs/works/task.md §4): poll with ``wait``/``task_status``,
        fetch rows with ``task_result``. Over HTTP a query that outlives the
        server's ``wait_ms`` (default 5s) escalates into a task the same way."""
        res = await self._exec.execute(Request(
            op="query", sql=sql, params=tuple(params or ()),
            ds_start=ds_start, ds_end=ds_end, as_task=as_task,
        ))
        if as_task or (isinstance(res, dict) and "task" in res):
            return res.id if isinstance(res, Task) else res["task"]
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

    async def task_status(self, task_id: str) -> Task:
        return await self._exec.execute(Request(op="status", ticket=task_id))

    write_status = task_status      # the old name (a ticket is a born-done task)

    async def tasks(self, *, limit: int = 50) -> list[Task]:
        """Recent tasks (writes, rebuilds, background queries), newest first."""
        return await self._exec.execute(Request(op="tasks", limit=limit))

    async def task_result(self, task_id: str) -> list[Row]:
        """Rows of a finished background query (from its result file)."""
        res = await self._exec.execute(Request(op="task_result", ticket=task_id))
        return res["rows"]

    async def cancel_task(self, task_id: str) -> Task:
        """Cancel a live background task (its record turns ``cancelled``; a duck
        phase already on a read thread runs out and is discarded — task.md §5)."""
        return await self._exec.execute(Request(op="task_cancel", ticket=task_id))

    async def wait(self, task_id: str, *, poll: float = 0.05) -> Task:
        """Block until the task settles (done/failed/cancelled), returning it."""
        while True:
            st = await self.task_status(task_id)
            if st.state not in ("pending", "running"):
                return st
            await asyncio.sleep(poll)

    # ─── streaming (embedded-only: resident unbounded pipelines) ───────

    async def stream(self, pipeline: str, *, name: str):
        """Start a resident streaming pipeline — ``watch '<glob>' | … |
        ingest <table>`` (docs/works/pipeline-streaming.md). Returns a
        :class:`StreamHandle` (``await handle.stop()`` to drain and stop).
        The stream only ingests; query the landed table with normal bounded
        SQL. Embedded form only."""
        if self._services is None:
            raise QueryError("stream() is embedded-only (open(), not connect())")
        return await self._services.stream.start_stream(pipeline, name=name)

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
