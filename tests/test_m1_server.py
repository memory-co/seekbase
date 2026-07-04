"""Server-form tests: the same QueryBuilder chain, driven over HTTP.

Uses httpx's in-process ASGITransport against the hand-rolled app — no port,
no uvicorn — so the full client -> wire -> server -> DuckDB round-trip is
exercised, including error-type propagation and the as-of write guard.
"""
from __future__ import annotations

import httpx
import pytest

from seekbase import NotSupportedYet, QueryError, ReadOnlyError, Seekbase
from seekbase.server import create_app

SCHEMA = {
    "cards": {
        "columns": {"card_id": "str primary", "issue": "str", "kind": "str", "n": "int"},
    },
}


async def _pair(tmp_path, *, api_key=None, as_of=None):
    """Return (server_db, client_db). Client talks to server over ASGI."""
    server_db = await Seekbase.open(tmp_path / "db", schema=SCHEMA)
    app = create_app(server_db, api_key=api_key)
    transport = httpx.ASGITransport(app=app)
    client_db = await Seekbase.connect(
        "http://server", api_key=api_key, as_of=as_of, transport=transport
    )
    return server_db, client_db


async def test_roundtrip_over_http(tmp_path):
    server_db, client = await _pair(tmp_path)
    await client.table("cards").insert(
        [{"card_id": f"c{i}", "issue": "x", "kind": "issue", "n": i} for i in range(3)]
    )
    rows = await client.table("cards").select("card_id", "n").gte("n", 1).order("n", desc=True)
    assert [r["n"] for r in rows] == [2, 1]
    assert await client.table("cards").count() == 3

    # written through the client, visible directly on the embedded server db
    assert await server_db.table("cards").count() == 3

    await client.close()
    await server_db.close()


async def test_delete_and_sql_over_http(tmp_path):
    server_db, client = await _pair(tmp_path)
    await client.table("cards").insert({"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
    assert await client.table("cards").delete().eq("card_id", "c1") == 1
    assert await client.table("cards").count() == 0
    raw = await client.sql("SELECT card_id, deleted_at FROM cards")
    assert raw[0]["deleted_at"] is not None
    await client.close()
    await server_db.close()


async def test_error_types_propagate(tmp_path):
    server_db, client = await _pair(tmp_path)
    with pytest.raises(QueryError):
        await client.table("cards").eq("nope", 1)          # unknown column -> 400 -> QueryError
    with pytest.raises(ReadOnlyError):
        await client.sql("DELETE FROM cards")               # non-read -> ReadOnlyError
    with pytest.raises(NotSupportedYet):
        await client.table("cards").search("x")             # search -> 501 -> NotSupportedYet
    await client.close()
    await server_db.close()


async def test_as_of_client_is_read_only(tmp_path):
    server_db, _ = await _pair(tmp_path)
    await server_db.table("cards").insert({"card_id": "c1", "issue": "x", "kind": "k", "n": 1})

    app = create_app(server_db)
    transport = httpx.ASGITransport(app=app)
    past = await Seekbase.connect(
        "http://server", as_of="2000-01-01T00:00:00+00:00", transport=transport
    )
    assert await past.table("cards").count() == 0            # rewound before the write
    with pytest.raises(ReadOnlyError):
        await past.table("cards").insert({"card_id": "c2", "issue": "y", "kind": "k", "n": 2})
    await past.close()
    await server_db.close()


async def test_auth_required(tmp_path):
    server_db, _ = await _pair(tmp_path, api_key="secret")
    app = create_app(server_db, api_key="secret")
    transport = httpx.ASGITransport(app=app)
    bad = await Seekbase.connect("http://server", api_key="wrong", transport=transport)
    with pytest.raises(Exception):
        await bad.table("cards").count()
    await bad.close()
    await server_db.close()


async def test_serve_runner_is_injected(tmp_path):
    # the ASGI runner is external — serve() calls whatever runner you pass,
    # so no uvicorn (or any runner dependency) is needed to drive it.
    from seekbase.server import serve

    server_db = await Seekbase.open(tmp_path / "db", schema=SCHEMA)
    captured = {}

    def fake_runner(app, *, host, port):
        captured["host"], captured["port"] = host, port
        captured["callable"] = callable(app)

    serve(server_db, host="1.2.3.4", port=9999, runner=fake_runner)
    assert captured == {"host": "1.2.3.4", "port": 9999, "callable": True}
    await server_db.close()


async def test_health(tmp_path):
    server_db = await Seekbase.open(tmp_path / "db", schema=SCHEMA)
    app = create_app(server_db)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://server"
    ) as c:
        resp = await c.get("/v1/health")
    assert resp.status_code == 200 and resp.json() == {"ready": True}
    await server_db.close()
