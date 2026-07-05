"""quickstart — 最基础的本地用法(端到端). See README.md.

纯本地、无 server、无 embedder:开库 → 写 → 查 → 删 → 再查。
"""
from __future__ import annotations

from seekbase import Seekbase

# 最小 schema:一张表、一个主键、一个文本列;没有 searchable → 不需要 embedder。
SCHEMA = [
    {
        "table": "notes",
        "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
        "primary": "id",
    },
]


async def test_open_write_read_delete_read(tmp_path):
    # 1) 在本地目录建库(进程内,DuckDB)
    db = await Seekbase.open(tmp_path / "db", schema=SCHEMA)
    try:
        # 2) 写入(异步:返 ticket,等它 done)
        await db.wait(await db.insert("notes", [{"id": "n1", "text": "hello"},
                                                {"id": "n2", "text": "world"}]))

        # 3) 查出来(SQL)
        rows = await db.query("SELECT id, text FROM notes ORDER BY id")
        assert rows == [{"id": "n1", "text": "hello"}, {"id": "n2", "text": "world"}]
        (count,) = (await db.query("SELECT count(*) AS c FROM notes"))
        assert count["c"] == 2

        # 4) 删一行(打墓碑)
        st = await db.wait(await db.delete("notes", where="id = ?", params=["n1"]))
        assert st["matched"] == 1

        # 5) 再查 —— 删掉的看不见了
        rows = await db.query("SELECT id, text FROM notes ORDER BY id")
        assert rows == [{"id": "n2", "text": "world"}]
    finally:
        await db.close()


async def test_data_persists_across_reopen(tmp_path):
    """本地库 = 一个目录:关掉再开同一目录,数据还在。"""
    path = tmp_path / "db"
    db = await Seekbase.open(path, schema=SCHEMA)
    await db.wait(await db.insert("notes", {"id": "n1", "text": "hello"}))
    await db.close()

    db2 = await Seekbase.open(path, schema=SCHEMA)
    try:
        assert await db2.query("SELECT id, text FROM notes") == [{"id": "n1", "text": "hello"}]
    finally:
        await db2.close()
