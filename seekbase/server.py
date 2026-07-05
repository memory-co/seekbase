"""seekbase server — exposes an embedded ``Seekbase`` over HTTP.

A minimal hand-rolled ASGI app (no web-framework dependency). Routes mirror
docs/api/:

  POST /v1/query            read (SQL + ds window) → {"rows": [...]}
  POST /v1/insert           write → {"ticket", "state", ...}
  POST /v1/delete           write → {"ticket", "state", "matched", ...}
  GET  /v1/writes/{ticket}  poll write status
  POST /v1/rebuild          admin (async ticket)
  POST /v1/vacuum           admin (async ticket)
  GET  /v1/health           {"ready": bool}

``seekbase_server(db)`` returns the ASGI app; the runner (uvicorn/…) is external
(``serve`` is a convenience). Auth is one optional bearer token.
"""
from __future__ import annotations

import json

from ._engine.plan import Request
from ._types import SeekbaseError
from ._wire import error_body, status_for
from .port import Seekbase


async def _read_json(receive) -> dict:
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body", False):
            break
    return json.loads(body) if body else {}


async def _send_json(send, status: int, obj) -> None:
    data = json.dumps(obj).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": data})


def _request_for(method: str, path: str, body: dict) -> Request:
    if method == "POST" and path == "/v1/query":
        return Request(op="query", sql=body.get("sql"),
                       params=tuple(body.get("params") or ()),
                       ds_start=body.get("ds_start"), ds_end=body.get("ds_end"))
    if method == "POST" and path == "/v1/insert":
        return Request(op="insert", table=body.get("table"),
                       rows=tuple(body.get("rows") or ()))
    if method == "POST" and path == "/v1/delete":
        return Request(op="delete", table=body.get("table"),
                       where=body.get("where"), params=tuple(body.get("params") or ()))
    if method == "POST" and path == "/v1/rebuild":
        return Request(op="rebuild")
    if method == "POST" and path == "/v1/vacuum":
        return Request(op="vacuum", before=body.get("before"))
    if method == "GET" and path.startswith("/v1/writes/"):
        return Request(op="status", ticket=path[len("/v1/writes/"):])
    return None


def seekbase_server(db: Seekbase, *, api_key: str | None = None):
    """Build an ASGI app serving ``db`` (a normal embedded Seekbase)."""

    async def app(scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        path, method = scope["path"], scope["method"]

        if api_key is not None:
            headers = dict(scope.get("headers", []))
            if headers.get(b"authorization", b"").decode() != f"Bearer {api_key}":
                return await _send_json(
                    send, 401, {"error": {"type": "Unauthorized", "message": "bad api key"}}
                )

        if method == "GET" and path == "/v1/health":
            return await _send_json(send, 200, {"ready": db.ready})

        req = _request_for(method, path, await _read_json(receive) if method == "POST" else {})
        if req is None:
            return await _send_json(send, 404, {"error": {"type": "NotFound", "message": path}})

        try:
            result = await db._dispatch(req)
        except SeekbaseError as e:
            return await _send_json(send, status_for(e), {"error": error_body(e)})
        except Exception as e:  # noqa: BLE001 - last-resort guard
            return await _send_json(send, 500, {"error": {"type": "Internal", "message": str(e)}})
        return await _send_json(send, 200, result)

    return app


def serve(db: Seekbase, *, host: str = "127.0.0.1", port: int = 8000,
          api_key: str | None = None, runner=None) -> None:
    """Convenience: serve ``db`` over HTTP. The ASGI runner is external — pass
    ``runner`` (any ``runner(app, host=, port=)``); defaults to uvicorn if
    installed, else a clear error pointing at ``seekbase_server(db)``."""
    app = seekbase_server(db, api_key=api_key)
    if runner is None:
        try:
            import uvicorn
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                "serve() needs an ASGI runner. `pip install uvicorn`, or pass "
                "runner=…, or run `seekbase_server(db)` under your own ASGI server."
            ) from e
        runner = uvicorn.run
    runner(app, host=host, port=port)
