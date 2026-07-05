"""insert_only — 只增、删即打墓碑 场景. See README.md."""
from __future__ import annotations

from seekbase import Seekbase


async def test_delete_is_a_tombstone(db):
    """delete() marks a tombstone: the row vanishes from query, and re-deleting
    the same key matches nothing (it's no longer live — not physically gone-then-
    absent, but already tombstoned)."""
    await db.wait(await db.insert("cards", {"card_id": "c1", "issue": "x", "kind": "k", "n": 1}))

    st = await db.wait(await db.delete("cards", where="card_id = ?", params=["c1"]))
    assert st["matched"] == 1

    # hidden from the normal read path
    (c,) = await db.query("SELECT count(*) AS c FROM cards")
    assert c["c"] == 0

    # re-deleting matches nothing: the row is already a tombstone, not re-live
    st2 = await db.wait(await db.delete("cards", where="card_id = ?", params=["c1"]))
    assert st2["matched"] == 0


async def test_port_has_no_update_path(db):
    """'改' is not first-class: the port exposes no update/upsert — values change
    only by append (a new version) + tombstone."""
    assert not hasattr(Seekbase, "update")
    assert not hasattr(Seekbase, "upsert")
