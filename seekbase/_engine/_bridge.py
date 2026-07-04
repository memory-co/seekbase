"""async ↔ sync bridge.

DuckDB is synchronous and single-writer. We own one dedicated worker thread
(a single-worker thread pool) that holds the DuckDB connection and serializes
every operation on it — satisfying both connection thread-affinity and the
single-writer model (DESIGN §6.4). All engine calls funnel through ``run``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")


class Bridge:
    def __init__(self, name: str = "seekbase") -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"{name}-duck"
        )
        self._closed = False

    async def run(self, fn: Callable[[], T]) -> T:
        """Run ``fn`` on the dedicated DuckDB thread and await its result."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._executor.shutdown(wait=True)
