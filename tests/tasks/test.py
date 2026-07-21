"""tasks — 统一操作句柄(task.md):写=出生即 done、rebuild=真后台 task、
as_task 查询 + 结果文件、HTTP wait_ms 超时升级(202 收编)、取消、tasks 列表。
See README.md."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from seekbase import QueryError, Seekbase
from seekbase.server import seekbase_server
from tests.conftest import FakeEmbedder, SCHEMA


async def _seed(db):
    await db.wait(await db.insert("cards", [
        {"card_id": "c1", "issue": "pty tmux terminal", "kind": "issue", "n": 1},
        {"card_id": "c2", "issue": "redis cache design", "kind": "design", "n": 2},
    ]))


# ─── 写 = 出生即 done 的 task(原 ticket,语义不变)───────────────────

async def test_write_is_a_born_done_task(db):
    tid = await db.insert("cards", {"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
    assert tid.startswith("tk_")
    st = await db.task_status(tid)
    assert (st.op, st.state) == ("insert", "done")     # synchronous: done on arrival
    assert st.submitted_at and st.finished_at


async def test_write_status_alias_still_works(db):
    tid = await db.insert("cards", {"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
    assert (await db.write_status(tid)).state == "done"


# ─── rebuild = 真 pending→done 后台 task ───────────────────────────────

async def test_rebuild_is_a_background_task(db):
    await _seed(db)
    rid = await db.rebuild()
    st = await db.wait(rid)                            # pending/running → done
    assert st.op == "rebuild" and st.state == "done"
    assert st.stats["rows"] == 2


# ─── as_task 查询:结果落文件、记录只存 query ─────────────────────────

async def test_as_task_query_and_result_file(db):
    await _seed(db)
    q = "search cards 'pty tmux' | SELECT card_id FROM _in ORDER BY _score DESC LIMIT 1"
    tid = await db.query(q, as_task=True)
    st = await db.wait(tid)
    assert st.state == "done" and st.op == "query"
    assert st.query == q                               # 表只记 query 文本
    assert st.rows == 1
    assert await db.task_result(tid) == [{"card_id": "c1"}]   # 行来自结果文件


async def test_task_result_before_done_rejected(db):
    await _seed(db)
    tid = await db.query("scan cards | SELECT * FROM _in", as_task=True)
    st = await db.task_status(tid)
    if st.state in ("pending", "running"):             # race-tolerant: may finish fast
        with pytest.raises(QueryError):
            await db.task_result(tid)
    await db.wait(tid)


async def test_failed_task_records_error(db):
    tid = await db.query("scan cards | SELECT nope_col FROM _in", as_task=True)
    st = await db.wait(tid)
    assert st.state == "failed" and st.error
    with pytest.raises(QueryError):
        await db.task_result(tid)


async def test_cancel_task(db):
    await _seed(db)
    tid = await db.query(
        "SELECT count(*) AS c FROM range(1, 3000000000)", as_task=True)
    await asyncio.sleep(0.05)
    st = await db.cancel_task(tid)
    assert st.state == "cancelled"
    with pytest.raises(QueryError):
        await db.task_result(tid)


# ─── tasks 列表(接口可查即可,不进 SQL 表)─────────────────────────────

async def test_tasks_lists_recent(db):
    await _seed(db)
    await db.wait(await db.query("scan cards | SELECT * FROM _in", as_task=True))
    ops = [t.op for t in await db.tasks(limit=10)]
    assert "insert" in ops and "query" in ops


# ─── HTTP:wait_ms 快路 200 零开销;超时 202 收编;as_task 202 ─────────

async def test_http_fast_path_creates_no_task(pair):
    server_db, client = pair
    before = len(await server_db.tasks(limit=50))
    assert await client.query("SELECT 1 AS x") == [{"x": 1}]
    assert len(await server_db.tasks(limit=50)) == before    # 快路零 task 开销


async def test_http_timeout_escalates_to_task(tmp_path):
    db = await Seekbase.open(tmp_path / "db", schema=SCHEMA, embedder=FakeEmbedder())
    try:
        await _seed(db)
        transport = httpx.ASGITransport(app=seekbase_server(db))
        hc = httpx.AsyncClient(transport=transport, base_url="http://s")
        r = await hc.post("/v1/query", json={
            "sql": "scan cards | SELECT count(*) AS c FROM _in", "wait_ms": 0})
        assert r.status_code == 202                    # 收编:查询继续跑,连接释放
        tid = r.json()["task"]
        for _ in range(100):
            s = (await hc.get(f"/v1/tasks/{tid}")).json()
            if s["state"] not in ("pending", "running"):
                break
            await asyncio.sleep(0.05)
        assert s["state"] == "done"
        rr = await hc.get(f"/v1/tasks/{tid}/result")
        assert rr.json() == {"rows": [{"c": 2}]}
        await hc.aclose()
    finally:
        await db.close()


async def test_http_as_task_roundtrip(pair):
    server_db, client = pair
    await _seed(server_db)
    tid = await client.query("scan cards | SELECT card_id FROM _in ORDER BY card_id",
                             as_task=True)
    st = await server_db.wait(tid)
    assert st.state == "done"
    assert [r["card_id"] for r in await client.task_result(tid)] == ["c1", "c2"]
    assert any(t.id == tid for t in await client.tasks())


# ─── close 时 runaway 被 interrupt,不挂死 ─────────────────────────────

async def test_runaway_task_does_not_hang_close(tmp_path):
    db = await Seekbase.open(tmp_path / "db", schema=SCHEMA, embedder=FakeEmbedder())
    await db.query("SELECT count(*) AS c FROM range(1, 3000000000)", as_task=True)
    await asyncio.sleep(0.1)
    await asyncio.wait_for(db.close(), timeout=15)     # interrupt-on-close 兜底
