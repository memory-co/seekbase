"""seekbase server — exposes an embedded ``Seekbase`` over HTTP (DESIGN §9).

A minimal hand-rolled ASGI app (no web framework dependency). Two routes:

- ``POST /v1/execute`` — run one serialized Request (the same unit the
  QueryBuilder builds), returning ``{"result": ...}`` or ``{"error": ...}``.
- ``GET  /v1/health``  — ``{"ready": bool}``.

``seekbase_server`` is the standard server surface — it has **no web-framework
dependency**. The ASGI *runner* is injected by the host: run the app under your
own uvicorn/hypercorn/gunicorn, mount it in a larger app, or use the ``serve()``
convenience (which just calls a runner you pass, defaulting to uvicorn if it's
installed). Tests drive the app in-process via an ASGI transport — no runner at
all. Auth is a single optional bearer token — multi-tenant auth is out of scope
(DESIGN §8).
"""
from __future__ import annotations

import json

from ._types import SeekbaseError
from ._wire import deserialize_request, error_body, status_for
from .port import Seekbase


async def _read_body(receive) -> bytes:
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body", False):
            return body


async def _send_json(send, status: int, obj: dict) -> None:
    data = json.dumps(obj).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": data})


def seekbase_server(db: Seekbase, *, api_key: str | None = None):
    """Build an ASGI app serving ``db``. ``db`` is a normal embedded Seekbase
    (opened with ``Seekbase.open``); the server holds the schema and embedder."""

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

        if method == "POST" and path == "/v1/execute":
            try:
                payload = json.loads(await _read_body(receive))
                req, as_of = deserialize_request(payload)
                result = await db._dispatch(req, as_of)
            except SeekbaseError as e:
                return await _send_json(send, status_for(e), {"error": error_body(e)})
            except Exception as e:  # noqa: BLE001 - last-resort guard
                return await _send_json(
                    send, 500, {"error": {"type": "Internal", "message": str(e)}}
                )
            return await _send_json(send, 200, {"result": result})

        return await _send_json(
            send, 404, {"error": {"type": "NotFound", "message": path}}
        )

    return app


def serve(
    db: Seekbase,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    api_key: str | None = None,
    runner=None,
) -> None:
    """Convenience: serve ``db`` over HTTP. Blocking — call from a launch script.

    The ASGI runner is external, not a seekbase dependency. Pass your own via
    ``runner`` — any callable ``runner(app, host=..., port=...)`` (e.g.
    ``uvicorn.run``). If omitted, uvicorn is used when importable; otherwise a
    clear error points you at ``seekbase_server(db)`` to run under any ASGI server.
    """
    app = seekbase_server(db, api_key=api_key)
    if runner is None:
        try:
            import uvicorn
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                "serve() needs an ASGI runner. Either `pip install uvicorn`, or "
                "pass runner=..., or run `seekbase_server(db)` under your own ASGI server."
            ) from e
        runner = uvicorn.run
    runner(app, host=host, port=port)
