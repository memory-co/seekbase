"""WriteService — the write use cases (insert / delete), with an explicit worker.

All writes funnel through one visible worker coroutine (``_worker_loop``): a
caller ``submit``s an op and awaits its completion Future (mode a — synchronous;
read-your-write holds because the batch's FTS index is rebuilt before the Future
resolves). The worker drains a **batch** off the queue and rebuilds each touched
table's FTS **once per batch** (FTS rebuild is O(table size)).

A write's completion record is a **born-done task** appended to the shared
TaskService log **last** (after files + db + FTS), so a done-task means the
whole write ran to completion — the old ticket, semantics unchanged
(docs/works/task.md §2).
"""
from __future__ import annotations

import asyncio
import contextlib

from .._types import QueryError
from ..runtime import now, today
from ..struct import Task
from .task_service import TaskService, _new_task_id


class WriteService:
    def __init__(self, store, embedding, files, schema, bridge, tasks: TaskService) -> None:
        self._store = store
        self._embedding = embedding
        self._files = files
        self._schema = schema
        self._bridge = bridge
        self._tasks = tasks                   # shared task log (writes append born-done)
        self._q: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._stop = False

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._worker_loop())

    async def close(self) -> None:
        self._stop = True
        if self._worker is not None:
            await self._q.put(None)
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker

    # ─── public: enqueue + await the completion (mode a, synchronous) ──

    async def insert(self, table: str, rows: list[dict], *, skip_existing: bool = False):
        """``skip_existing`` = the idempotent streaming-sink mode: duplicate
        primary keys are dropped instead of raising (at-least-once replays
        dedupe here, docs/works/pipeline-streaming.md §7)."""
        return await self._submit(("insert", table, list(rows), skip_existing))

    async def delete(self, table: str, where: str | None, params):
        if not where:
            raise QueryError("delete requires a where clause")
        return await self._submit(("delete", table, where, list(params)))

    async def _submit(self, op):
        fut = asyncio.get_running_loop().create_future()
        await self._q.put((op, fut))
        return await fut

    # ─── the write worker: drain a batch, execute, FTS once, log tickets ─

    async def _worker_loop(self) -> None:
        while not self._stop:
            first = await self._q.get()
            if first is None:
                break
            batch = [first]
            while not self._q.empty():
                item = self._q.get_nowait()
                if item is None:
                    self._stop = True
                    break
                batch.append(item)
            try:
                await self._process_batch(batch)
            except Exception as e:                # noqa: BLE001 - never let the loop die
                for _, fut in batch:
                    if not fut.done():
                        fut.set_exception(e)

    async def _process_batch(self, batch) -> None:
        executed = []                             # (fut, kind, extra)
        fts_tables: set[str] = set()
        for op, fut in batch:
            try:
                kind, extra, fts_table = await self._execute_one(op)
                if fts_table:
                    fts_tables.add(fts_table)
                executed.append((fut, kind, extra))
            except Exception as e:
                if not fut.done():
                    fut.set_exception(e)
        for t in fts_tables:                      # FTS once per touched table, before resolving
            with contextlib.suppress(Exception):
                await self._store.rebuild_fts(t)
        # the task record is the LAST step: done-task ⟹ the whole write completed
        ts = now()
        tasks = [Task(id=_new_task_id(), op=kind, state="done",
                      submitted_at=ts, finished_at=ts, **extra)
                 for _, kind, extra in executed]
        for task in tasks:
            await self._tasks.append(task)
        for (fut, _, _), task in zip(executed, tasks):
            if not fut.done():
                fut.set_result(task)

    async def _execute_one(self, op):
        kind = op[0]
        if kind == "insert":
            _, table, rows, skip_existing = op
            records = await self._store.validate(
                table, rows, skip_existing=skip_existing)              # cols + write-once pk
            spec = self._schema.table(table)
            if not records:                                            # everything deduped away
                return "insert", {}, None
            vecs, toks = ({}, {})
            if self._embedding is not None:
                vecs, toks = await self._embedding.embed_records(spec, records)
            ds, ts = today(), now()
            await self._files.write_puts(spec, records, ds, ts)        # canonical, files first
            await self._store.commit_rows(spec, records, vecs, toks, ds, ts, rebuild_fts=False)
            await self._store.index_search_rows(spec, records, vecs, toks)   # lance append (vss: no-op)
            return "insert", {}, (table if self._embedding is not None else None)
        if kind == "delete":
            _, table, where, params = op
            keys = await self._store.match_live(table, where, params)
            ds, ts = today(), now()
            await self._files.write_deletes(table, keys, ds, ts)       # tombstone lines, files first
            await self._store.soft_delete(table, keys, ds, ts)         # deleted filtered by ds; no FTS
            return "delete", {"matched": len(keys)}, None
        raise QueryError(f"unknown write op {kind!r}")

    # ─── status: delegated to the shared task log ──────────────────────

    async def status(self, task_id: str) -> Task:
        return await self._tasks.status(task_id)
