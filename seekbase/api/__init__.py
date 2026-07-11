"""HTTP API surface — one module per endpoint.

The directory listing is the API: each ``<name>.py`` declares one ``Endpoint``
(method + path + async handler) and documents its contract in the module
docstring; they mirror ``docs/api/*.md`` one-to-one.

  query.py     POST /v1/query            read (SQL + ds window)
  insert.py    POST /v1/insert           write rows (synchronous)
  delete.py    POST /v1/delete           soft-delete rows
  writes.py    GET  /v1/writes/{ticket}  poll a write's status
  rebuild.py   POST /v1/rebuild          admin: replay files → DuckDB
  health.py    GET  /v1/health           readiness probe

``server.py`` builds the ASGI app from ``ENDPOINTS`` and dispatches with
``resolve(method, path)``.
"""
from __future__ import annotations

from . import delete, health, insert, query, rebuild, writes
from ._route import Endpoint, match_path

ENDPOINTS: list[Endpoint] = [
    query.ENDPOINT,
    insert.ENDPOINT,
    delete.ENDPOINT,
    writes.ENDPOINT,
    rebuild.ENDPOINT,
    health.ENDPOINT,
]


def resolve(method: str, path: str) -> tuple[Endpoint | None, dict]:
    """Find the endpoint for ``method``/``path`` and its captured path params."""
    for ep in ENDPOINTS:
        if ep.method == method:
            params = match_path(ep.path, path)
            if params is not None:
                return ep, params
    return None, {}


__all__ = ["Endpoint", "ENDPOINTS", "resolve"]
