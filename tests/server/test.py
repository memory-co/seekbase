"""server — server 形态:同一条链走 HTTP 场景. See README.md."""
from __future__ import annotations

import httpx
import pytest

from seekbase import NotSupportedYet, QueryError, ReadOnlyError, SeekbaseError
from seekbase.server import serve
from tests.conftest import client_for, open_db

# ────────── round-trip parity ──────────


async def test_same_chain_over_http(pair):
    server_db, client = pair
    await client.table("cards").insert(
        [{"card_id": f"c{i}", "issue": "x", "kind": "issue", "n": i} for i in range(3)]
    )
    rows = await client.table("cards").select("card_id", "n").gte("n", 1).order("n", desc=True)
    assert [r["n"] for r in rows] == [2, 1]
    assert await client.table("cards").count() == 3

    # written through the client, visible directly on the embedded server db
    assert await server_db.table("cards").count() == 3


async def test_delete_and_sql_over_http(pair):
    _, client = pair
    await client.table("cards").insert({"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
    assert await client.table("cards").delete().eq("card_id", "c1") == 1
    assert await client.table("cards").count() == 0
    raw = await client.sql("SELECT card_id, deleted_at FROM cards")
    assert raw[0]["deleted_at"] is not None


# ────────── error typing / auth / health ──────────


async def test_error_types_propagate(pair):
    _, client = pair
    with pytest.raises(QueryError):
        await client.table("cards").eq("nope", 1)       # unknown column -> 400
    with pytest.raises(ReadOnlyError):
        await client.sql("DELETE FROM cards")            # non-read -> 400
    with pytest.raises(NotSupportedYet):
        await client.table("cards").search("x")          # search -> 501


async def test_as_of_client_is_read_only(tmp_path):
    server_db = await open_db(tmp_path)
    await server_db.table("cards").insert({"card_id": "c1", "issue": "x", "kind": "k", "n": 1})

    past = await client_for(server_db, as_of="2000-01-01T00:00:00+00:00")
    try:
        assert await past.table("cards").count() == 0    # rewound before the write
        with pytest.raises(ReadOnlyError):
            await past.table("cards").insert(
                {"card_id": "c2", "issue": "y", "kind": "k", "n": 2}
            )
    finally:
        await past.close()
        await server_db.close()


async def test_wrong_api_key_is_rejected(tmp_path):
    server_db = await open_db(tmp_path)
    bad = await client_for(server_db, app_key="secret", client_key="wrong")
    try:
        with pytest.raises(SeekbaseError):
            await bad.table("cards").count()
    finally:
        await bad.close()
        await server_db.close()


async def test_health_endpoint(tmp_path):
    from seekbase.server import seekbase_server

    server_db = await open_db(tmp_path)
    app = seekbase_server(server_db)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://server"
    ) as c:
        resp = await c.get("/v1/health")
    assert resp.status_code == 200 and resp.json() == {"ready": True}
    await server_db.close()


# ────────── runner is external ──────────


async def test_serve_uses_injected_runner(tmp_path):
    """The ASGI runner is external — serve() calls whatever runner you pass, so
    no uvicorn (or any runner dependency) is needed to drive it."""
    server_db = await open_db(tmp_path)
    captured = {}

    def fake_runner(app, *, host, port):
        captured.update(host=host, port=port, callable=callable(app))

    serve(server_db, host="1.2.3.4", port=9999, runner=fake_runner)
    assert captured == {"host": "1.2.3.4", "port": 9999, "callable": True}
    await server_db.close()
