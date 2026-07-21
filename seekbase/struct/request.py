"""Request — the transport-neutral unit that flows through an executor.

Built by the port; run either straight against the services (LocalExecutor) or
serialized to an HTTP endpoint (HttpExecutor). One shape both forms agree on:

  read:   query (as_task → a background task id)
  write:  insert / delete
  tasks:  status / tasks / task_result / task_cancel
  admin:  rebuild
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Request:
    op: str
    # query
    sql: str | None = None
    params: tuple = ()
    ds_start: str | None = None
    ds_end: str | None = None
    # writes
    table: str | None = None
    rows: tuple[dict, ...] = ()        # insert
    where: str | None = None          # delete
    # query-as-task
    as_task: bool = False             # explicit background query (task.md §4)
    # tasks
    ticket: str | None = None         # status / task_result / task_cancel: the task id
    limit: int = 50                   # tasks (list)
