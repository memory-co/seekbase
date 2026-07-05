"""basic_orm — 核心结构化读写 round-trip 场景. See README.md."""
from __future__ import annotations

from tests.conftest import open_db

# ────────── insert / select / count happy path ──────────


async def test_insert_then_select_returns_row(db):
    await db.table("cards").insert(
        {"card_id": "c1", "issue": "pty tmux", "kind": "issue", "n": 3}
    )
    rows = await db.table("cards").select("card_id", "issue").eq("kind", "issue")
    assert rows == [{"card_id": "c1", "issue": "pty tmux"}]


async def test_default_select_includes_created_at(db):
    """A bare select() projects the declared columns plus the engine-managed
    created_at — you see the write time without declaring it."""
    await db.table("cards").insert({"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
    (row,) = await db.table("cards").select()
    assert set(row) == {"card_id", "issue", "kind", "n", "created_at"}
    assert row["created_at"]  # auto-stamped


async def test_filters_order_and_paging(db):
    await db.table("cards").insert(
        [{"card_id": f"c{i}", "issue": "i", "kind": "issue", "n": i} for i in range(5)]
    )
    rows = await db.table("cards").select("n").gte("n", 2).order("n", desc=True).limit(2)
    assert [r["n"] for r in rows] == [4, 3]

    page = await db.table("cards").select("n").order("n").limit(2).offset(2)
    assert [r["n"] for r in page] == [2, 3]


async def test_count_variants(db):
    await db.table("cards").insert(
        [{"card_id": f"c{i}", "issue": "i", "kind": "issue", "n": i} for i in range(5)]
    )
    assert await db.table("cards").count() == 5
    assert await db.table("cards").in_("card_id", ["c0", "c1"]).count() == 2
    assert await db.table("cards").like("card_id", "c%").count() == 5
    assert await db.table("cards").in_("card_id", []).count() == 0  # IN () matches nothing


# ────────── lifecycle ──────────


async def test_context_manager_opens_and_closes(tmp_path):
    async with await open_db(tmp_path) as db:
        await db.table("cards").insert({"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
        assert await db.table("cards").count() == 1
