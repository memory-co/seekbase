"""Task endpoints — the unified operation handles (docs/works/task.md).

  GET  /v1/tasks               recent tasks (newest first, fixed window)
  GET  /v1/tasks/{id}          one task's state
  GET  /v1/tasks/{id}/result   a finished background query's rows
  POST /v1/tasks/{id}/cancel   cancel a live background task

``/v1/writes/{ticket}`` stays as a compatibility alias for ``/v1/tasks/{id}``
(a ticket is a born-done task).
"""
from __future__ import annotations

from ._route import Endpoint


async def _list(db, body: dict, params: dict) -> tuple[int, dict]:
    tasks = await db.services.task.list()
    return 200, {"tasks": [t.to_wire() for t in tasks]}


async def _status(db, body: dict, params: dict) -> tuple[int, dict]:
    return 200, (await db.services.task.status(params["id"])).to_wire()


async def _result(db, body: dict, params: dict) -> tuple[int, dict]:
    return 200, {"rows": await db.services.task.result(params["id"])}


async def _cancel(db, body: dict, params: dict) -> tuple[int, dict]:
    return 200, (await db.services.task.cancel(params["id"])).to_wire()


LIST = Endpoint("GET", "/v1/tasks", _list)
STATUS = Endpoint("GET", "/v1/tasks/{id}", _status)
RESULT = Endpoint("GET", "/v1/tasks/{id}/result", _result)
CANCEL = Endpoint("POST", "/v1/tasks/{id}/cancel", _cancel)
