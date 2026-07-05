"""read_write — SQL 读 + 异步写 round-trip 场景. See README.md."""
from __future__ import annotations

from tests.conftest import open_db


async def _seed(db, rows):
    await db.wait(await db.insert("cards", rows))


async def test_insert_then_query(db):
    await _seed(db, {"card_id": "c1", "issue": "pty tmux", "kind": "issue", "n": 3})
    rows = await db.query("SELECT card_id, issue FROM cards WHERE kind = ?", params=["issue"])
    assert rows == [{"card_id": "c1", "issue": "pty tmux"}]


async def test_batch_filters_order_limit(db):
    await _seed(db, [{"card_id": f"c{i}", "issue": "i", "kind": "issue", "n": i}
                     for i in range(5)])
    rows = await db.query("SELECT n FROM cards WHERE n >= 2 ORDER BY n DESC LIMIT 2")
    assert [r["n"] for r in rows] == [4, 3]
    (c,) = await db.query("SELECT count(*) AS c FROM cards WHERE card_id IN ('c0','c1')")
    assert c["c"] == 2


async def test_reinsert_same_key_is_latest_wins(db):
    await _seed(db, {"card_id": "c1", "issue": "v1", "kind": "k", "n": 1})
    await _seed(db, {"card_id": "c1", "issue": "v2", "kind": "k", "n": 2})
    rows = await db.query("SELECT issue, n FROM cards")
    assert rows == [{"issue": "v2", "n": 2}]  # new version replaces old


async def test_insert_ticket_settles_done(db):
    ticket = await db.insert("cards", {"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
    st = await db.wait(ticket)
    assert st["ticket"] == ticket and st["state"] == "done"


async def test_context_manager(tmp_path):
    async with await open_db(tmp_path) as db:
        await db.wait(await db.insert("cards", {"card_id": "c1", "issue": "x", "kind": "k", "n": 1}))
        (c,) = await db.query("SELECT count(*) AS c FROM cards")
        assert c["c"] == 1
