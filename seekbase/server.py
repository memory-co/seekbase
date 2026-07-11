"""seekbase server — exposes an embedded ``Seekbase`` over HTTP.

A minimal hand-rolled ASGI shell (no web-framework dependency): auth, body
read/write, error mapping, and dispatch to the endpoint modules in
``seekbase/api/`` (one file per route — that directory *is* the API surface).

``seekbase_server(db)`` returns the ASGI app; the runner (uvicorn/…) is external
(``serve`` is a convenience). Auth is one optional bearer token.
"""
from __future__ import annotations

import json

from ._types import SeekbaseError
from ._wire import error_body, status_for
from .api import resolve
from .client import Seekbase


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


def seekbase_server(db: Seekbase, *, api_key: str | None = None):
    """Build an ASGI app serving ``db`` (a normal embedded Seekbase). Routing +
    per-endpoint logic live in ``seekbase/api/``; this shell just wires them."""

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

        endpoint, params = resolve(method, path)
        if endpoint is None:
            return await _send_json(send, 404, {"error": {"type": "NotFound", "message": path}})

        body = await _read_json(receive) if method == "POST" else {}
        try:
            status, result = await endpoint.handle(db, body, params)
        except SeekbaseError as e:
            return await _send_json(send, status_for(e), {"error": error_body(e)})
        except Exception as e:  # noqa: BLE001 - last-resort guard
            return await _send_json(send, 500, {"error": {"type": "Internal", "message": str(e)}})
        return await _send_json(send, status, result)

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
