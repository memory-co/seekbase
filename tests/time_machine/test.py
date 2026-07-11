"""time_machine — ds 时间窗 场景. See README.md.

M1:ds = 写入日;`ds_start`/`ds_end` 按分区列裁剪。行都写于「今天」,所以用远
过去 / 远未来的边界来断言窗口语义。
"""
from __future__ import annotations

import pytest

from seekbase import ReadOnlyError


async def _seed(db):
    await db.wait(await db.insert("cards", {"card_id": "c1", "issue": "x", "kind": "k", "n": 1}))


async def test_ds_end_before_any_write_sees_nothing(db):
    await _seed(db)
    (c,) = await db.query("SELECT count(*) AS c FROM cards", ds_end="20000101")
    assert c["c"] == 0                      # 时光机:那天什么都还没有


async def test_ds_end_in_future_sees_rows(db):
    await _seed(db)
    (c,) = await db.query("SELECT count(*) AS c FROM cards", ds_end="20990101")
    assert c["c"] == 1


async def test_ds_start_in_future_sees_nothing(db):
    await _seed(db)
    (c,) = await db.query("SELECT count(*) AS c FROM cards", ds_start="20990101")
    assert c["c"] == 0


async def test_ds_window_excluding_today_is_empty(db):
    await _seed(db)
    (c,) = await db.query(
        "SELECT count(*) AS c FROM cards", ds_start="20000101", ds_end="20000107"
    )
    assert c["c"] == 0


async def test_query_is_read_only(db):
    with pytest.raises(ReadOnlyError):
        await db.query("DELETE FROM cards")
    with pytest.raises(ReadOnlyError):
        await db.query("UPDATE cards SET n = 0")


async def test_bad_ds_format_rejected(db):
    from seekbase import QueryError
    with pytest.raises(QueryError):
        await db.query("SELECT * FROM cards", ds_end="2026-06-01")


async def test_time_travel_across_create_and_delete(tmp_path):
    """One physical row per key (write-once); the time machine is a ds/delete-
    horizon filter: visible as-of D iff created ds<=D and not-yet-deleted then.
    Seeds a cross-day create→delete directly at the engine (the public API
    always writes 'today').

    n1: created@day02 → deleted@day05.
    """
    from seekbase._engine.bridge import Bridge
    from seekbase._engine.duck import DuckdbEngine
    from seekbase.schema import parse_schema

    bridge = Bridge()
    schema = parse_schema([{
        "table": "notes",
        "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
        "primary": "id",
    }])
    eng = await DuckdbEngine.open(tmp_path, schema, bridge)

    def _seed():
        c = eng._conn
        c.execute("INSERT INTO _sb_notes (id, text, ds, created_at) "
                  "VALUES ('n1','v1','20260102','t')")
        c.execute("UPDATE _sb_notes SET deleted_ds='20260105', deleted_at='t' WHERE id='n1'")
    await bridge.run(_seed)

    async def q(ds_end):
        return await eng.run_query("SELECT id, text FROM notes", [], None, ds_end)

    try:
        assert await q("20260101") == []                            # day1: not created yet
        assert await q("20260103") == [{"id": "n1", "text": "v1"}]   # day3: alive
        assert await q("20260106") == []                            # day6: deleted
        assert await q(None) == []                                   # now: deleted
    finally:
        await eng.close()
        bridge.close()
