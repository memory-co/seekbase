"""quickstart — 最基础的本地用法(端到端). See README.md.

纯本地、无 server、无 embedder:开库 → 写 → 查 → 删 → 再查。
"""
from __future__ import annotations

from seekbase import Seekbase

# 最小 schema:一张表、一个主键、一个文本列;没有 searchable → 不需要 embedder。
SCHEMA = {
    "notes": {
        "columns": {"id": "str primary", "text": "str"},
    },
}


async def test_open_write_read_delete_read(tmp_path):
    # 1) 在本地目录建库(进程内,DuckDB)
    db = await Seekbase.open(tmp_path / "db", schema=SCHEMA)
    try:
        # 2) 写入
        await db.table("notes").insert({"id": "n1", "text": "hello"})
        await db.table("notes").insert({"id": "n2", "text": "world"})

        # 3) 查出来
        rows = await db.table("notes").select("id", "text").order("id")
        assert rows == [
            {"id": "n1", "text": "hello"},
            {"id": "n2", "text": "world"},
        ]
        assert await db.table("notes").count() == 2

        # 4) 删一行(打墓碑)
        deleted = await db.table("notes").delete().eq("id", "n1")
        assert deleted == 1

        # 5) 再查 —— 删掉的看不见了
        rows = await db.table("notes").select("id", "text").order("id")
        assert rows == [{"id": "n2", "text": "world"}]
        assert await db.table("notes").count() == 1
    finally:
        await db.close()


async def test_data_persists_across_reopen(tmp_path):
    """本地库 = 一个目录:关掉再开同一目录,数据还在。"""
    path = tmp_path / "db"

    db = await Seekbase.open(path, schema=SCHEMA)
    await db.table("notes").insert({"id": "n1", "text": "hello"})
    await db.close()

    db2 = await Seekbase.open(path, schema=SCHEMA)
    try:
        rows = await db2.table("notes").select("id", "text")
        assert rows == [{"id": "n1", "text": "hello"}]
    finally:
        await db2.close()
