"""WriteService — the write use cases (insert / delete), with an explicit worker
and the ticket concept living here (no standalone TicketService).

All writes funnel through one visible worker coroutine (``_worker_loop``): a
caller ``submit``s an op and awaits its completion Future (mode a — synchronous;
read-your-write holds because the batch's FTS index is rebuilt before the Future
resolves). The worker drains a **batch** off the queue and rebuilds each touched
table's FTS **once per batch** (FTS rebuild is O(table size)).

A **ticket** is a write's completion record — issued **last** (after files + db +
FTS), so a done-ticket means the whole write ran to completion. Tickets are
appended to a durable, status-only log ``<data_dir>/tickets/ds=YYYYMMDD.jsonl``
(id embeds the ds so ``status`` goes straight to the partition). See ticket.md.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from pathlib import Path

from .._types import NotFound, QueryError
from ..runtime import now, today
from ..struct import Ticket


def _new_ticket_id() -> str:
    return f"wr_{today()}_{uuid.uuid4().hex[:12]}"        # ds-embedded → self-locating


def _ds_of(ticket_id: str) -> str | None:
    parts = ticket_id.split("_")
    return parts[1] if len(parts) >= 3 and parts[0] == "wr" else None


class WriteService:
    def __init__(self, store, embedding, files, schema, bridge, tickets_dir) -> None:
        self._store = store
        self._embedding = embedding
        self._files = files
        self._schema = schema
        self._bridge = bridge                 # ticket-log append/status run here (single writer)
        self._tickets_dir = Path(tickets_dir)
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

    async def insert(self, table: str, rows: list[dict]):
        return await self._submit(("insert", table, list(rows)))

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
        # ticket is the LAST step: a done-ticket ⟹ the whole write completed
        tickets = [Ticket(id=_new_ticket_id(), op=kind, **extra) for _, kind, extra in executed]
        await self._append(tickets)
        for (fut, _, _), ticket in zip(executed, tickets):
            if not fut.done():
                fut.set_result(ticket)

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
            return "insert", {}, (table if self._embedding is not None else None)
        if kind == "delete":
            _, table, where, params = op
            keys = await self._store.match_live(table, where, params)
            ds, ts = today(), now()
            await self._files.write_deletes(table, keys, ds, ts)       # tombstone lines, files first
            await self._store.soft_delete(table, keys, ds, ts)         # deleted filtered by ds; no FTS
            return "delete", {"matched": len(keys)}, None
        raise QueryError(f"unknown write op {kind!r}")

    # ─── ticket log (durable, status-only, ds-partitioned JSONL) ───────

    async def issue(self, op: str, *, matched=None, stats=None) -> Ticket:
        """Issue + log one ticket (used by admin/rebuild, outside the worker)."""
        t = Ticket(id=_new_ticket_id(), op=op, matched=matched, stats=stats)
        await self._append([t])
        return t

    async def status(self, ticket_id: str) -> Ticket:
        ds = _ds_of(ticket_id)
        if ds is None:
            raise NotFound(f"unknown ticket {ticket_id!r}")
        path = self._tickets_dir / f"ds={ds}.jsonl"

        def _find():
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        d = json.loads(line)
                        if d.get("ticket") == ticket_id:
                            return Ticket.from_wire(d)
            return None

        found = await self._bridge.run(_find)
        if found is None:
            raise NotFound(f"unknown ticket {ticket_id!r}")
        return found

    async def _append(self, tickets: list[Ticket]) -> None:
        if not tickets:
            return

        def _do():
            by_ds: dict[str, list[Ticket]] = {}
            for t in tickets:
                by_ds.setdefault(_ds_of(t.id) or today(), []).append(t)
            for ds, group in by_ds.items():
                p = self._tickets_dir / f"ds={ds}.jsonl"
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "a", encoding="utf-8") as f:
                    for t in group:
                        f.write(json.dumps(t.to_wire(), ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())

        await self._bridge.run(_do)
