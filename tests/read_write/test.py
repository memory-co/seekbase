"""read_write — SQL 读 + 异步写 round-trip 场景. See README.md."""
from __future__ import annotations

import asyncio

import pytest

from seekbase import QueryError
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


async def test_concurrent_inserts_batch_and_all_land(db):
    """All writes funnel through the single write worker; concurrent inserts are
    drained as a batch and every one lands (read-your-write holds right after)."""
    tickets = await asyncio.gather(*[
        db.insert("cards", {"card_id": f"c{i}", "issue": f"i{i}", "kind": "k", "n": i})
        for i in range(10)])
    assert len(tickets) == 10 and all(t for t in tickets)
    (c,) = await db.query("SELECT count(*) AS c FROM cards")
    assert c["c"] == 10                      # every concurrent write is durable + visible


async def test_reads_run_concurrently_with_writes(db):
    """Reads run on the ReadPool (cursors, MVCC) — concurrent with the single
    write worker. Interleaved reads/writes don't error; each read sees a valid
    snapshot and all writes converge."""
    results = await asyncio.gather(*(
        [db.insert("cards", {"card_id": f"c{i}", "issue": "x", "kind": "k", "n": i})
         for i in range(15)]
        + [db.query("SELECT count(*) AS c FROM cards") for _ in range(15)]))
    reads = [r for r in results if isinstance(r, list)]
    assert all(r[0]["c"] <= 15 for r in reads)       # every read saw a valid snapshot
    (final,) = await db.query("SELECT count(*) AS c FROM cards")
    assert final["c"] == 15                          # all writes landed


async def test_reinsert_same_key_errors(db):
    """Primary keys are write-once: re-inserting an existing key is rejected
    at the (funnelled) write path; the original row is untouched."""
    await _seed(db, {"card_id": "c1", "issue": "v1", "kind": "k", "n": 1})
    with pytest.raises(QueryError):
        await _seed(db, {"card_id": "c1", "issue": "v2", "kind": "k", "n": 2})
    rows = await db.query("SELECT issue, n FROM cards")
    assert rows == [{"issue": "v1", "n": 1}]  # original kept, re-insert rejected


async def test_insert_ticket_settles_done(db):
    ticket = await db.insert("cards", {"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
    st = await db.wait(ticket)
    assert st.id == ticket and st.state == "done"


async def test_context_manager(tmp_path):
    async with await open_db(tmp_path) as db:
        await db.wait(await db.insert("cards", {"card_id": "c1", "issue": "x", "kind": "k", "n": 1}))
        (c,) = await db.query("SELECT count(*) AS c FROM cards")
        assert c["c"] == 1
