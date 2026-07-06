"""server — server 形态:同一套调用走 HTTP 场景. See README.md."""
from __future__ import annotations

import httpx
import pytest

from seekbase import NotFound, QueryError, ReadOnlyError, SeekbaseError
from seekbase.server import seekbase_server, serve
from tests.conftest import client_for, open_db


async def test_same_calls_over_http(pair):
    server_db, client = pair
    await client.wait(await client.insert("cards", [
        {"card_id": f"c{i}", "issue": "x", "kind": "issue", "n": i} for i in range(3)
    ]))
    rows = await client.query("SELECT card_id, n FROM cards WHERE n >= 1 ORDER BY n DESC")
    assert [r["n"] for r in rows] == [2, 1]

    # written through the client, visible directly on the embedded server db
    (c,) = await server_db.query("SELECT count(*) AS c FROM cards")
    assert c["c"] == 3


async def test_delete_over_http(pair):
    _, client = pair
    await client.wait(await client.insert("cards", {"card_id": "c1", "issue": "x", "kind": "k", "n": 1}))
    st = await client.wait(await client.delete("cards", where="card_id = ?", params=["c1"]))
    assert st["matched"] == 1
    (c,) = await client.query("SELECT count(*) AS c FROM cards")
    assert c["c"] == 0


async def test_error_types_propagate(pair):
    _, client = pair
    with pytest.raises(ReadOnlyError):
        await client.query("DELETE FROM cards")            # non-read -> 400
    with pytest.raises(QueryError):
        await client.query("SELECT * FROM nope")           # unknown table -> 400
    with pytest.raises(NotFound):
        await client.write_status("wr_missing")            # unknown ticket -> 404


async def test_wrong_api_key_rejected(tmp_path):
    server_db = await open_db(tmp_path)
    bad = await client_for(server_db, app_key="secret", client_key="wrong")
    try:
        with pytest.raises(SeekbaseError):
            await bad.query("SELECT 1 AS x")
    finally:
        await bad.close()
        await server_db.close()


async def test_health_endpoint(tmp_path):
    server_db = await open_db(tmp_path)
    app = seekbase_server(server_db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://server") as c:
        resp = await c.get("/v1/health")
    assert resp.status_code == 200 and resp.json() == {"ready": True}
    await server_db.close()


async def test_serve_uses_injected_runner(tmp_path):
    server_db = await open_db(tmp_path)
    captured = {}

    def fake_runner(app, *, host, port):
        captured.update(host=host, port=port, callable=callable(app))

    serve(server_db, host="1.2.3.4", port=9999, runner=fake_runner)
    assert captured == {"host": "1.2.3.4", "port": 9999, "callable": True}
    await server_db.close()
