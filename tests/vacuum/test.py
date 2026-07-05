"""vacuum — 显式丢历史(按行清死行)场景. See README.md."""
from __future__ import annotations

import pytest

from seekbase import QueryError, Seekbase
from tests.conftest import open_db

NOTES = [
    {"table": "notes",
     "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
     "primary": "id"},
]


async def _seed_cards(db):
    await db.wait(await db.insert("cards", [
        {"card_id": f"c{i}", "issue": "x", "kind": "k", "n": i} for i in range(1, 4)
    ]))


async def test_vacuum_purges_only_rows_dead_before(db):
    await _seed_cards(db)
    await db.wait(await db.delete("cards", where="card_id = 'c1'"))

    # deleted today → a past `before` purges nothing
    st = await db.wait(await db.vacuum(before="20000101"))
    assert st["stats"]["purged"] == 0

    # a future `before` purges the dead row; live rows untouched
    st = await db.wait(await db.vacuum(before="20990101"))
    assert st["stats"]["purged"] == 1
    ids = [r["card_id"] for r in await db.query("SELECT card_id FROM cards ORDER BY card_id")]
    assert ids == ["c2", "c3"]


async def test_vacuum_removes_history_from_files(tmp_path):
    path = tmp_path / "db"
    db = await open_db(tmp_path, schema=NOTES, embedder=None)
    await db.wait(await db.insert("notes", [{"id": "n1", "text": "a"}, {"id": "n2", "text": "b"}]))
    await db.wait(await db.delete("notes", where="id = ?", params=["n1"]))
    await db.wait(await db.vacuum(before="20990101"))
    await db.close()

    # rebuild from files: the purged row's insert + tombstone are gone → it stays gone
    (path / "duck.db").unlink()
    db2 = await Seekbase.open(path, schema=NOTES)
    try:
        await db2.wait(await db2.rebuild())
        assert await db2.query("SELECT id, text FROM notes") == [{"id": "n2", "text": "b"}]
    finally:
        await db2.close()


async def test_vacuum_bad_ds_rejected(db):
    with pytest.raises(QueryError):
        await db.vacuum(before="2026-01-01")
