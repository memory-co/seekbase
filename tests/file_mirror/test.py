"""file_mirror — canonical 文件镜像 + rebuild 场景. See README.md."""
from __future__ import annotations

import json

from seekbase import Seekbase

SCHEMA = [
    {"table": "notes",
     "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
     "primary": "id"},
]


def _lines(data_dir):
    """All jsonl records under files/, across ds partitions, in file order."""
    out = []
    for p in sorted((data_dir / "files").rglob("*.jsonl")):
        for line in p.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


async def test_insert_appends_full_row_line(tmp_path):
    db = await Seekbase.open(tmp_path / "db", schema=SCHEMA)
    try:
        await db.wait(await db.insert("notes", {"id": "n1", "text": "hello"}))
        recs = _lines(tmp_path / "db")
        assert len(recs) == 1
        r = recs[0]
        assert r["id"] == "n1" and r["text"] == "hello"
        assert r["ds"] and r["created_at"] and "_deleted" not in r  # full snapshot + meta, a put
    finally:
        await db.close()


async def test_delete_appends_tombstone_event(tmp_path):
    db = await Seekbase.open(tmp_path / "db", schema=SCHEMA)
    try:
        await db.wait(await db.insert("notes", {"id": "n1", "text": "x"}))
        await db.wait(await db.delete("notes", where="id = ?", params=["n1"]))
        recs = _lines(tmp_path / "db")
        assert any(r.get("_deleted") == "n1" for r in recs)   # tombstone appended
        assert recs[0].get("_deleted") is None                # original row unchanged
    finally:
        await db.close()


async def test_rebuild_restores_exact_state_from_files(tmp_path):
    path = tmp_path / "db"
    db = await Seekbase.open(path, schema=SCHEMA)
    await db.wait(await db.insert("notes", [{"id": "n1", "text": "a"}, {"id": "n2", "text": "b"}]))
    await db.wait(await db.delete("notes", where="id = ?", params=["n1"]))
    await db.close()

    # wipe the derived DuckDB; reopen sees nothing until rebuild
    (path / "duck.db").unlink()
    db2 = await Seekbase.open(path, schema=SCHEMA)
    try:
        (empty,) = await db2.query("SELECT count(*) AS c FROM notes")
        assert empty["c"] == 0

        st = await db2.wait(await db2.rebuild())
        assert st.state == "done"

        rows = await db2.query("SELECT id, text FROM notes ORDER BY id")
        assert rows == [{"id": "n2", "text": "b"}]   # n1 deleted (soft), n2 survives
    finally:
        await db2.close()
