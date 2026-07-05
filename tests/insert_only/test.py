"""insert_only — 只增、删即打墓碑 场景. See README.md."""
from __future__ import annotations

from seekbase import Seekbase


async def test_delete_is_a_tombstone_not_a_physical_delete(db):
    """delete() marks deleted_at. The row vanishes from normal queries but
    physically survives — history is never erased."""
    await db.table("cards").insert({"card_id": "c1", "issue": "x", "kind": "k", "n": 1})

    n = await db.table("cards").delete().eq("card_id", "c1")
    assert n == 1

    # hidden from the normal read path...
    assert await db.table("cards").count() == 0
    assert await db.table("cards").select().eq("card_id", "c1") == []

    # ...but the row is still on disk, carrying its tombstone
    raw = await db.sql("SELECT card_id, deleted_at FROM cards")
    assert len(raw) == 1
    assert raw[0]["card_id"] == "c1"
    assert raw[0]["deleted_at"] is not None


async def test_port_has_no_update_path(db):
    """'改' is not a first-class op: the port exposes no update/upsert — the
    only way values change is append + tombstone."""
    assert not hasattr(db.table("cards"), "update")
    assert not hasattr(db.table("cards"), "upsert")
    assert not hasattr(Seekbase, "update")
