"""search — 语义检索(SQL 里的 search() 函数)场景. See README.md."""
from __future__ import annotations

import pytest

from seekbase import QueryError
from tests.conftest import open_db


async def _seed(db):
    await db.wait(await db.insert("cards", [
        {"card_id": "c1", "issue": "why pty makes users think of tmux", "kind": "issue", "n": 1},
        {"card_id": "c2", "issue": "redis vs local cache choice", "kind": "design", "n": 2},
        {"card_id": "c3", "issue": "pty tmux terminal multiplexer", "kind": "issue", "n": 3},
    ]))


async def test_search_ranks_by_similarity(db):
    await _seed(db)
    hits = await db.query(
        "SELECT card_id, _score FROM cards WHERE search('pty tmux terminal') ORDER BY _score DESC")
    ids = [h["card_id"] for h in hits]
    assert ids[0] == "c3"                 # closest
    assert ids[-1] == "c2"                # redis is least relevant
    scores = [h["_score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


async def test_search_combines_with_structured_filter(db):
    await _seed(db)
    hits = await db.query(
        "SELECT card_id FROM cards WHERE search('cache redis') AND kind = 'design' "
        "ORDER BY _score DESC LIMIT 1")
    assert hits == [{"card_id": "c2"}]


async def test_deleted_row_is_not_searchable(db):
    await _seed(db)
    await db.wait(await db.delete("cards", where="card_id = ?", params=["c3"]))
    hits = await db.query(
        "SELECT card_id FROM cards WHERE search('pty tmux terminal') ORDER BY _score DESC")
    assert "c3" not in [h["card_id"] for h in hits]


async def test_search_respects_time_window(db):
    await _seed(db)
    assert await db.query("SELECT card_id FROM cards WHERE search('pty tmux')", ds_end="20990101")
    assert await db.query("SELECT card_id FROM cards WHERE search('pty tmux')", ds_end="20000101") == []


async def test_search_needs_a_searchable_table(tmp_path):
    schema = [{"table": "notes",
               "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
               "primary": "id"}]
    db = await open_db(tmp_path, schema=schema, embedder=None)
    try:
        with pytest.raises(QueryError):
            await db.query("SELECT * FROM notes WHERE search('x')")
    finally:
        await db.close()
