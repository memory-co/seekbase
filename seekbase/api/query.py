"""POST /v1/query — read: run a pipeline (SQL by default, + ds time window)
→ ``{"rows": […]}``.

A query is ``stage | stage``: a segment whose leading token hits the operator
registry (``search``/``scan``/``grep``) is that operator, anything else is one
DuckDB SQL statement; a pure SQL query has zero pipes. The ``ds_start`` /
``ds_end`` time machine applies to the whole pipeline (search candidates
included). Read-only: non-SELECT → ``ReadOnlyError``. See docs/api/query.md.
"""
from __future__ import annotations

import asyncio

from ._route import Endpoint

_DEFAULT_WAIT_MS = 5000


async def handle(db, body: dict, params: dict) -> tuple[int, dict]:
    """Bounded wait, then escalate (docs/works/task.md §4): the query runs up
    to ``wait_ms`` (default 5s) → 200 rows with zero task overhead; if it is
    still running, it is *adopted* into a task mid-flight (it keeps running,
    the connection is released) → 202 {task, state}. ``as_task: true`` skips
    the wait and returns 202 immediately."""
    sql = body.get("sql")
    run = lambda: db.services.read.query(          # noqa: E731
        sql, body.get("params") or [], body.get("ds_start"), body.get("ds_end"))
    if body.get("as_task"):
        task = await db.services.task.submit("query", run, query=sql)
        return 202, task.to_wire()
    fut = asyncio.ensure_future(run())
    done, _ = await asyncio.wait({fut}, timeout=float(body.get("wait_ms", _DEFAULT_WAIT_MS)) / 1000)
    if done:
        return 200, fut.result()                   # raises through to the error mapper
    task = await db.services.task.adopt("query", fut, query=sql)
    return 202, task.to_wire()


ENDPOINT = Endpoint("POST", "/v1/query", handle)
