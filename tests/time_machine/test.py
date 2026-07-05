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
