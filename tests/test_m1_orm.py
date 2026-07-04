"""M1 contract tests: structured ORM on DuckDB, tombstone-delete, read-only
SQL passthrough, partial as-of time machine, and schema validation."""
from __future__ import annotations

import pytest

from seekbase import (
    EmbedderInvalid,
    NotSupportedYet,
    QueryError,
    ReadOnlyError,
    SchemaError,
    Seekbase,
)

SCHEMA = {
    "cards": {
        "columns": {
            "card_id": "str primary",
            "issue": "str",
            "kind": "str",
            "n": "int",
        },
        "searchable": ["issue"],
    },
}


async def _open(tmp_path, **kw):
    return await Seekbase.open(tmp_path / "db", schema=SCHEMA, **kw)


# ─── a fake embedder so searchable schemas open without a real model ───
class FakeEmbedder:
    dim = 8

    def embed(self, texts):
        return [[float(len(t))] * self.dim for t in texts]


async def test_insert_select_roundtrip(tmp_path):
    db = await _open(tmp_path, embedder=FakeEmbedder())
    await db.table("cards").insert({"card_id": "c1", "issue": "pty tmux", "kind": "issue", "n": 3})
    rows = await db.table("cards").select("card_id", "issue").eq("kind", "issue")
    assert rows == [{"card_id": "c1", "issue": "pty tmux"}]
    await db.close()


async def test_default_select_includes_created_at(tmp_path):
    db = await _open(tmp_path, embedder=FakeEmbedder())
    await db.table("cards").insert({"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
    rows = await db.table("cards").select()
    assert set(rows[0]) == {"card_id", "issue", "kind", "n", "created_at"}
    assert rows[0]["created_at"]  # auto-stamped
    await db.close()


async def test_filters_order_limit(tmp_path):
    db = await _open(tmp_path, embedder=FakeEmbedder())
    await db.table("cards").insert(
        [{"card_id": f"c{i}", "issue": "i", "kind": "issue", "n": i} for i in range(5)]
    )
    rows = await (
        db.table("cards").select("n").gte("n", 2).order("n", desc=True).limit(2)
    )
    assert [r["n"] for r in rows] == [4, 3]

    assert await db.table("cards").in_("card_id", ["c0", "c1"]).count() == 2
    assert await db.table("cards").like("card_id", "c%").count() == 5
    await db.close()


async def test_delete_is_tombstone(tmp_path):
    db = await _open(tmp_path, embedder=FakeEmbedder())
    await db.table("cards").insert({"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
    n = await db.table("cards").delete().eq("card_id", "c1")
    assert n == 1
    # hidden from normal queries...
    assert await db.table("cards").count() == 0
    # ...but the row physically survives with a tombstone (visible via raw SQL)
    raw = await db.sql('SELECT card_id, deleted_at FROM cards')
    assert len(raw) == 1 and raw[0]["deleted_at"] is not None
    await db.close()


async def test_sql_is_read_only(tmp_path):
    db = await _open(tmp_path, embedder=FakeEmbedder())
    with pytest.raises(ReadOnlyError):
        await db.sql("DELETE FROM cards")
    with pytest.raises(ReadOnlyError):
        await db.sql("UPDATE cards SET n = 0")
    await db.close()


async def test_as_of_is_read_only_and_rewinds(tmp_path):
    db = await _open(tmp_path, embedder=FakeEmbedder())
    await db.table("cards").insert({"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
    # capture a timestamp strictly after the write
    (t,) = (await db.sql("SELECT created_at FROM cards"))[0].values()
    await db.close()

    past = await _open(tmp_path, embedder=FakeEmbedder(), as_of="2000-01-01T00:00:00+00:00")
    assert await past.table("cards").count() == 0  # nothing existed back then
    with pytest.raises(ReadOnlyError):
        await past.table("cards").insert({"card_id": "c2", "issue": "y", "kind": "k", "n": 2})
    await past.close()

    now = await _open(tmp_path, embedder=FakeEmbedder(), as_of=t)
    assert await now.table("cards").count() == 1  # visible as of its own creation
    await now.close()


async def test_search_operator_accepted_but_deferred(tmp_path):
    db = await _open(tmp_path, embedder=FakeEmbedder())
    with pytest.raises(NotSupportedYet):
        await db.table("cards").search("pty tmux").eq("kind", "issue")
    await db.close()


async def test_unknown_column_rejected(tmp_path):
    db = await _open(tmp_path, embedder=FakeEmbedder())
    with pytest.raises(QueryError):
        await db.table("cards").eq("nope", 1)
    with pytest.raises(QueryError):
        await db.table("cards").insert({"card_id": "c1", "bogus": 1})
    await db.close()


async def test_searchable_requires_embedder(tmp_path):
    with pytest.raises(EmbedderInvalid):
        await Seekbase.open(tmp_path / "db", schema=SCHEMA)  # no embedder


def test_schema_validation():
    with pytest.raises(SchemaError):  # no primary key
        from seekbase.schema import parse_schema
        parse_schema({"t": {"columns": {"a": "str"}}})
    from seekbase.schema import parse_schema
    with pytest.raises(SchemaError):  # reserved metadata column
        parse_schema({"t": {"columns": {"id": "str primary", "created_at": "str"}}})
    with pytest.raises(SchemaError):  # bad type
        parse_schema({"t": {"columns": {"id": "str primary", "x": "blob"}}})
    with pytest.raises(SchemaError):  # files placeholder not a column
        parse_schema({"t": {"columns": {"id": "str primary"}, "files": "x/{missing}.json"}})


async def test_context_manager(tmp_path):
    async with await _open(tmp_path, embedder=FakeEmbedder()) as db:
        await db.table("cards").insert({"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
        assert await db.table("cards").count() == 1
