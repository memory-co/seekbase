"""Service layer — every file here is a ``*_service.py``. Two kinds:

  domain services (own a subdomain end-to-end)
    store_service.py      StoreService     — the DuckDB engine: structured + pluggable search backend (one connection)
    embedding_service.py  EmbeddingService — text → vectors + tokens (wraps the injected Embedder + jieba)
    file_service.py       FileService      — canonical file mirror: record/tombstone shapes

  use-case services (thin orchestrators: order + policy only)
    pipeline_service.py PipelineService — read: SPL pipeline compiler (authorize → assign runtimes → fuse → plan)
    write_service.py    WriteService    — insert / delete via one worker (appends born-done tasks)
    task_service.py     TaskService     — unified operation handles: log + result files + background runner + GC
    stream_service.py   StreamService   — resident unbounded pipelines (watch | … | ingest), at-least-once + idempotent sink
    admin_service.py    AdminService    — rebuild: replay the file mirror, as a background task

The old **ticket** is a born-done task in the shared TaskService log
(docs/works/task.md §2). ``build_services`` wires the use-case services onto the domain ones — one shared
operator :class:`Registry` (built-ins + user operators) and one :class:`Policy`
serve both the query compiler and the stream runtime. The local execution seam
(LocalExecutor) lives in ``client.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..operator import Registry, builtin_operators
from ..operator.policy import Policy
from .admin_service import AdminService
from .embedding_service import EmbeddingService
from .file_service import FileService
from .pipeline_service import PipelineService
from .store_service import StoreService
from .stream_service import StreamHandle, StreamService
from .task_service import TaskService
from .write_service import WriteService


@dataclass(frozen=True)
class Services:
    read: PipelineService
    write: WriteService
    admin: AdminService
    stream: StreamService
    task: TaskService


def build_services(store, embedding, files, schema, bridge, tasks_dir,
                   policy: Policy | None = None, operators: list | None = None) -> Services:
    policy = policy or Policy()
    registry = Registry()
    for op in builtin_operators():
        registry.register(op)
    for op in operators or []:                    # user operators: classes or instances
        registry.register(op() if isinstance(op, type) else op)
    tasks = TaskService(bridge, Path(tasks_dir))
    write = WriteService(store, embedding, files, schema, bridge, tasks)
    return Services(
        read=PipelineService(store, embedding, schema, registry, policy),
        write=write,
        admin=AdminService(store, embedding, files, schema, tasks),   # rebuild = background task
        stream=StreamService(write, schema, registry, policy,
                             Path(tasks_dir).parent / "streams"),
        task=tasks,
    )


__all__ = [
    "Services", "build_services",
    "StoreService", "EmbeddingService", "FileService",
    "PipelineService", "WriteService", "AdminService",
    "StreamService", "StreamHandle", "TaskService",
]
