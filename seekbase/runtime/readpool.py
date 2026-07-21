"""ReadPool — concurrent reads, off the single-writer bridge.

Reads run on a small thread pool, each borrowing a DuckDB **cursor** (a
connection sharing the write instance). Via MVCC they run concurrently with —
and are not blocked by — the single writer / FTS rebuild (verified: a read during
a long write returns immediately on its snapshot). The write side keeps using the
single-writer :class:`Bridge`; only reads come here.

Cursors are created on the bridge thread (where the write connection lives) and
handed to read threads; each cursor is checked out by one read at a time.
"""
from __future__ import annotations

import asyncio
import queue
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")


class ReadPool:
    def __init__(self, cursors: list, pool: ThreadPoolExecutor) -> None:
        self._free: queue.Queue = queue.Queue()
        for c in cursors:
            self._free.put(c)
        self._all = list(cursors)     # held for close-time interrupt (incl. checked-out ones)
        self._pool = pool
        self._closed = False

    @classmethod
    async def create(cls, bridge, conn, workers: int = 4) -> "ReadPool":
        # cursors must be created where the write connection lives — the bridge thread
        cursors = await bridge.run(lambda: [conn.cursor() for _ in range(workers)])
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="seekbase-read")
        return cls(cursors, pool)

    async def run(self, fn: Callable[..., T]) -> T:
        """Run ``fn(cursor)`` on a read thread with a borrowed cursor."""
        loop = asyncio.get_running_loop()

        def _do() -> T:
            cur = self._free.get()
            try:
                return fn(cur)
            finally:
                self._free.put(cur)

        return await loop.run_in_executor(self._pool, _do)

    def close(self) -> None:
        """Interrupt every cursor first (cross-thread safe in DuckDB), so a
        runaway query cannot hang shutdown — the read thread gets an
        InterruptException and returns its cursor; then join the pool."""
        if not self._closed:
            self._closed = True
            for c in self._all:
                try:
                    c.interrupt()
                except Exception:     # noqa: BLE001 — best-effort, close anyway
                    pass
            self._pool.shutdown(wait=True)
