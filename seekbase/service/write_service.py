"""WriteService — the write use cases (insert / delete), with an explicit worker.

All writes funnel through one visible worker coroutine (``_worker_loop``): a
caller ``submit``s an op and awaits its completion Future (mode a — synchronous;
read-your-write holds because the batch's FTS index is rebuilt before the Future
resolves). The worker drains a **batch** off the queue and rebuilds each touched
table's FTS **once per batch** (FTS rebuild is O(table size), so amortizing it is
the win over per-insert).

Per op it delegates each subdomain's logic — StoreService (validate/dup-pk, row
commit), EmbeddingService (embed + tokenize), FileService (files-first record /
tombstone shapes) — and issues a ``Ticket``. Primary keys are write-once.
The `bridge` under StoreService/FileService still offloads the blocking DuckDB /
file calls to its thread; the queue + Futures here stay on the event loop.
"""
from __future__ import annotations

import asyncio
import contextlib

from .._types import QueryError
from ..runtime import now, today


class WriteService:
    def __init__(self, store, embedding, files, schema, tickets) -> None:
        self._store = store
        self._embedding = embedding
        self._files = files
        self._schema = schema
        self._tickets = tickets
        self._q: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._stop = False

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._worker_loop())

    async def close(self) -> None:
        self._stop = True
        if self._worker is not None:
            await self._q.put(None)                 # sentinel: drain then stop
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker

    # ─── public: enqueue + await the completion (mode a, synchronous) ──

    async def insert(self, table: str, rows: list[dict]):
        return await self._submit(("insert", table, list(rows)))

    async def delete(self, table: str, where: str | None, params):
        if not where:
            raise QueryError("delete requires a where clause")   # fail fast, before queuing
        return await self._submit(("delete", table, where, list(params)))

    async def _submit(self, op):
        fut = asyncio.get_running_loop().create_future()
        await self._q.put((op, fut))
        return await fut

    # ─── the write worker: drain a batch, execute, FTS once, resolve ───

    async def _worker_loop(self) -> None:
        while not self._stop:
            first = await self._q.get()
            if first is None:                        # close sentinel
                break
            batch = [first]
            while not self._q.empty():               # grab the rest without blocking
                item = self._q.get_nowait()
                if item is None:
                    self._stop = True
                    break
                batch.append(item)
            try:
                await self._process_batch(batch)
            except Exception as e:                   # noqa: BLE001 - never let the loop die
                for _, fut in batch:
                    if not fut.done():
                        fut.set_exception(e)

    async def _process_batch(self, batch) -> None:
        executed = []                                # (fut, kind, extra) for successes
        fts_tables: set[str] = set()
        for op, fut in batch:
            try:
                kind, extra, fts_table = await self._execute_one(op)
                if fts_table:
                    fts_tables.add(fts_table)
                executed.append((fut, kind, extra))
            except Exception as e:                   # per-op error → only that caller sees it
                if not fut.done():
                    fut.set_exception(e)
        # FTS once per touched table, BEFORE resolving → read-your-write holds
        for t in fts_tables:
            with contextlib.suppress(Exception):     # write already committed; stale FTS is best-effort
                await self._store.rebuild_fts(t)
        for fut, kind, extra in executed:
            if not fut.done():
                fut.set_result(self._tickets.issue(kind, **extra))

    async def _execute_one(self, op):
        kind = op[0]
        if kind == "insert":
            _, table, rows = op
            records = await self._store.validate(table, rows)          # cols + write-once pk
            spec = self._schema.table(table)
            vecs, toks = ({}, {})
            if self._embedding is not None:
                vecs, toks = await self._embedding.embed_records(spec, records)
            ds, ts = today(), now()
            await self._files.write_puts(spec, records, ds, ts)        # canonical, files first
            await self._store.commit_rows(spec, records, vecs, toks, ds, ts, rebuild_fts=False)
            # only inserts to a searchable table need the FTS rebuild (deferred to batch end)
            return "insert", {}, (table if self._embedding is not None else None)
        if kind == "delete":
            _, table, where, params = op
            keys = await self._store.match_live(table, where, params)
            ds, ts = today(), now()
            await self._files.write_deletes(table, keys, ds, ts)       # tombstone lines, files first
            await self._store.soft_delete(table, keys, ds, ts)         # deleted rows filtered by ds; no FTS rebuild
            return "delete", {"matched": len(keys)}, None
        raise QueryError(f"unknown write op {kind!r}")
