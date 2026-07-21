"""TaskService — the unified operation-handle subdomain (docs/works/task.md).

Owns the task log (ds-partitioned JSONL, ``<data_dir>/tasks/ds=YYYYMMDD.jsonl``,
state transitions appended — **last line wins**), the result files
(``tasks/results/<id>.jsonl`` — the record stores only the query text, rows go
to a file), retention GC, and the background runner for real pending→done
tasks (rebuild, ``as_task`` queries, HTTP timeout escalation via ``adopt``).

Writes stay mode-a synchronous: WriteService appends **born-done** tasks here
(the old ticket, semantics unchanged). Cancellation is honest: it cancels the
asyncio wrapper and marks the record ``cancelled`` — a duck phase already
executing on a ReadPool thread runs to completion and its result is discarded
(task.md §5).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from pathlib import Path

from .._types import NotFound, QueryError
from ..runtime import now, today
from ..struct import Task

__all__ = ["TaskService"]

TASK_TIMEOUT = 300.0          # max runtime for a background task (task.md §5)
LOG_RETENTION_DAYS = 30       # task log partitions
RESULT_RETENTION_DAYS = 7     # result files (results die younger than records)


def _new_task_id() -> str:
    return f"tk_{today()}_{uuid.uuid4().hex[:12]}"        # ds-embedded → self-locating


def _ds_of(task_id: str) -> str | None:
    parts = task_id.split("_")
    return parts[1] if len(parts) >= 3 and parts[1].isdigit() else None


class TaskService:
    def __init__(self, bridge, tasks_dir: Path, *, task_timeout: float = TASK_TIMEOUT) -> None:
        self._bridge = bridge                      # log IO runs on the bridge thread
        self._dir = Path(tasks_dir)
        self._results = self._dir / "results"
        self._timeout = task_timeout
        self._live: dict[str, asyncio.Task] = {}   # running background wrappers

    # ─── log primitives (append-only; last line per id wins) ───────────

    async def append(self, task: Task) -> Task:
        """Append one state line (also how a new task is born)."""
        path = self._dir / f"ds={_ds_of(task.id)}.jsonl"
        line = json.dumps(task.to_wire(), ensure_ascii=False)

        def _do() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        await self._bridge.run(_do)
        return task

    async def status(self, task_id: str) -> Task:
        ds = _ds_of(task_id)
        if ds is None:
            raise NotFound(f"unknown task {task_id!r}")
        path = self._dir / f"ds={ds}.jsonl"

        def _find():
            found = None
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        d = json.loads(line)
                        if d.get("task") == task_id or d.get("ticket") == task_id:
                            found = d                      # keep scanning: last wins
            return found

        found = await self._bridge.run(_find)
        if found is None:
            raise NotFound(f"unknown task {task_id!r}")
        return Task.from_wire(found)

    async def list(self, limit: int = 50) -> list[Task]:
        """Recent tasks, newest partitions first, last state per id."""
        def _scan():
            out: dict[str, dict] = {}
            for path in sorted(self._dir.glob("ds=*.jsonl"), reverse=True):
                if len(out) >= limit * 3:                  # enough partitions read
                    break
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            d = json.loads(line)
                            out[d.get("task") or d.get("ticket")] = d
            return list(out.values())
        rows = await self._bridge.run(_scan)
        tasks = [Task.from_wire(d) for d in rows]
        tasks.sort(key=lambda t: t.submitted_at or "", reverse=True)
        return tasks[:limit]

    # ─── background runner: pending → running → done/failed/cancelled ──

    async def submit(self, op: str, coro_fn, *, query: str | None = None) -> Task:
        """Start a background task. ``coro_fn()`` → for ``op="query"`` a list of
        rows (persisted to the result file); otherwise a ``stats`` dict."""
        task = Task(id=_new_task_id(), op=op, state="pending",
                    query=query, submitted_at=now())
        await self.append(task)
        wrapper = asyncio.create_task(self._drive(task, coro_fn))
        self._live[task.id] = wrapper
        wrapper.add_done_callback(lambda _: self._live.pop(task.id, None))
        return task

    async def adopt(self, op: str, fut: asyncio.Future, *, query: str | None = None) -> Task:
        """Escalate an already-running future into a task (the HTTP wait_ms
        timeout path, task.md §4): the fast path pays zero task overhead — a
        record is only born the moment escalation happens."""
        task = Task(id=_new_task_id(), op=op, state="running",
                    query=query, submitted_at=now())
        await self.append(task)
        wrapper = asyncio.create_task(self._drive(task, lambda: fut, running=True))
        self._live[task.id] = wrapper
        wrapper.add_done_callback(lambda _: self._live.pop(task.id, None))
        return task

    async def _drive(self, task: Task, coro_fn, *, running: bool = False) -> None:
        from dataclasses import replace
        try:
            if not running:
                task = replace(task, state="running")
                await self.append(task)
            result = await asyncio.wait_for(coro_fn(), timeout=self._timeout)
            if task.op == "query":
                rows = result["rows"] if isinstance(result, dict) else result
                n = await self._persist_result(task.id, rows)
                await self.append(replace(task, state="done", rows=n, finished_at=now()))
            else:
                await self.append(replace(
                    task, state="done", stats=result, finished_at=now()))
        except asyncio.CancelledError:
            await self.append(replace(task, state="cancelled", finished_at=now()))
        except asyncio.TimeoutError:
            await self.append(replace(
                task, state="failed", finished_at=now(),
                error=f"exceeded max task runtime ({self._timeout:.0f}s)"))
        except Exception as e:                     # noqa: BLE001 — recorded, not raised
            await self.append(replace(
                task, state="failed", error=f"{type(e).__name__}: {e}"[:500],
                finished_at=now()))

    async def wait_live(self, task_id: str, timeout: float) -> bool:
        """Fast path: if the task is live in this process, wait up to
        ``timeout`` seconds for it to settle. Returns True if it settled."""
        wrapper = self._live.get(task_id)
        if wrapper is None:
            return True
        done, _ = await asyncio.wait({wrapper}, timeout=timeout)
        return bool(done)

    async def cancel(self, task_id: str) -> Task:
        wrapper = self._live.get(task_id)
        if wrapper is not None:
            wrapper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wrapper
        return await self.status(task_id)

    # ─── results: files, not table rows (task.md §1) ───────────────────

    async def _persist_result(self, task_id: str, rows: list[dict]) -> int:
        path = self._results / f"{task_id}.jsonl"

        def _do() -> int:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            tmp.replace(path)
            return len(rows)
        return await self._bridge.run(_do)

    async def result(self, task_id: str) -> list[dict]:
        task = await self.status(task_id)
        if task.state in ("pending", "running"):
            raise QueryError(f"task {task_id} is still {task.state}")
        if task.state == "cancelled":
            raise QueryError(f"task {task_id} was cancelled")
        if task.state == "failed":
            raise QueryError(f"task {task_id} failed: {task.error}")
        if task.op != "query":
            raise QueryError(f"task {task_id} ({task.op}) has no result rows")
        path = self._results / f"{task_id}.jsonl"

        def _read():
            if not path.exists():
                raise QueryError(f"task {task_id} result expired (retention GC)")
            with open(path, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        return await self._bridge.run(_read)

    # ─── retention (lazy, at open; no background thread) ───────────────

    async def gc(self) -> None:
        def _do() -> None:
            cutoff_log = time.time() - LOG_RETENTION_DAYS * 86400
            cutoff_res = time.time() - RESULT_RETENTION_DAYS * 86400
            for p in self._dir.glob("ds=*.jsonl"):
                if p.stat().st_mtime < cutoff_log:
                    p.unlink(missing_ok=True)
            if self._results.exists():
                for p in self._results.glob("*.jsonl"):
                    if p.stat().st_mtime < cutoff_res:
                        p.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            await self._bridge.run(_do)

    async def close(self) -> None:
        for wrapper in list(self._live.values()):
            wrapper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await wrapper
