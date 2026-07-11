"""search — 语义检索(SQL 里的 search(column, '文本') 函数)场景. See README.md."""
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
        "SELECT card_id, _score FROM cards WHERE search(issue, 'pty tmux terminal') ORDER BY _score DESC")
    ids = [h["card_id"] for h in hits]
    assert ids[0] == "c3"                 # closest
    assert ids[-1] == "c2"                # redis is least relevant
    assert [h["_score"] for h in hits] == sorted((h["_score"] for h in hits), reverse=True)


async def test_search_combines_with_structured_filter(db):
    await _seed(db)
    hits = await db.query(
        "SELECT card_id FROM cards WHERE search(issue, 'cache redis') AND kind = 'design' "
        "ORDER BY _score DESC LIMIT 1")
    assert hits == [{"card_id": "c2"}]


async def test_per_column_search_is_independent(tmp_path):
    """Each searchable column has its own vector index: the same query text
    against different columns can rank differently."""
    schema = [{
        "table": "docs",
        "columns": [{"name": "id", "type": "str"},
                    {"name": "title", "type": "str"}, {"name": "body", "type": "str"}],
        "primary": "id",
        "searchable": ["title", "body"],
    }]
    db = await open_db(tmp_path, schema=schema)
    try:
        await db.wait(await db.insert("docs", [
            {"id": "d1", "title": "tmux terminal panes", "body": "redis cache eviction"},
            {"id": "d2", "title": "redis cache eviction", "body": "tmux terminal panes"},
        ]))
        top_title = (await db.query(
            "SELECT id FROM docs WHERE search(title, 'tmux terminal panes') ORDER BY _score DESC LIMIT 1"))[0]
        top_body = (await db.query(
            "SELECT id FROM docs WHERE search(body, 'tmux terminal panes') ORDER BY _score DESC LIMIT 1"))[0]
        assert top_title["id"] == "d1"    # title match
        assert top_body["id"] == "d2"     # body match — same text, different column, different row
    finally:
        await db.close()


async def test_multiple_searches_expose_per_column_scores(tmp_path):
    """Two search() in one query → a `_score_<col>` column each; a single
    search still exposes bare `_score` (backward-compatible convenience)."""
    schema = [{
        "table": "docs",
        "columns": [{"name": "id", "type": "str"},
                    {"name": "title", "type": "str"}, {"name": "body", "type": "str"}],
        "primary": "id",
        "searchable": ["title", "body"],
    }]
    db = await open_db(tmp_path, schema=schema)
    try:
        await db.wait(await db.insert("docs", [
            {"id": "d1", "title": "tmux terminal panes", "body": "redis cache eviction"},
            {"id": "d2", "title": "redis cache eviction", "body": "tmux terminal panes"},
        ]))
        # single search → bare _score works
        assert "_score" in (await db.query(
            "SELECT id, _score FROM docs WHERE search(title, 'tmux') LIMIT 1"))[0]

        # two searches → one score column per column
        rows = await db.query(
            "SELECT id, _score_title, _score_body FROM docs "
            "WHERE search(title, 'tmux terminal panes') OR search(body, 'tmux terminal panes') "
            "ORDER BY id")
        by_id = {r["id"]: r for r in rows}
        # d1 matches on title, d2 matches on body — the higher score is on the matching column
        assert by_id["d1"]["_score_title"] > by_id["d1"]["_score_body"]
        assert by_id["d2"]["_score_body"] > by_id["d2"]["_score_title"]
    finally:
        await db.close()


async def test_chinese_hybrid_search(tmp_path):
    """中文:search() = vss(向量语义)+ fts(BM25 关键词,jieba 分词)RRF 融合。
    关键词命中由 BM25 保证,和 embedder 语义质量无关。"""
    schema = [{
        "table": "notes",
        "columns": [{"name": "id", "type": "str"}, {"name": "body", "type": "str"}],
        "primary": "id",
        "searchable": ["body"],
    }]
    db = await open_db(tmp_path, schema=schema)
    try:
        await db.wait(await db.insert("notes", [
            {"id": "n1", "body": "为什么伪终端 pty 会让人联想到 tmux 终端复用器"},
            {"id": "n2", "body": "Redis 缓存淘汰策略 LRU 与 LFU 的对比"},
            {"id": "n3", "body": "机器学习里的向量嵌入与近邻相似度检索"},
        ]))
        hits = await db.query(
            "SELECT id, _score FROM notes WHERE search(body, '缓存淘汰') ORDER BY _score DESC")
        assert hits[0]["id"] == "n2"          # '缓存淘汰' 经 jieba→BM25 命中 n2
        assert all(h["_score"] is not None for h in hits)
    finally:
        await db.close()


async def test_deleted_row_is_not_searchable(db):
    await _seed(db)
    await db.wait(await db.delete("cards", where="card_id = ?", params=["c3"]))
    hits = await db.query(
        "SELECT card_id FROM cards WHERE search(issue, 'pty tmux terminal') ORDER BY _score DESC")
    assert "c3" not in [h["card_id"] for h in hits]


async def test_rebuild_repopulates_search(tmp_path):
    """rebuild() clears the derived vss+fts projection and replays the file
    mirror; the consumer must repopulate it so search() works again."""
    schema = [{
        "table": "notes",
        "columns": [{"name": "id", "type": "str"}, {"name": "body", "type": "str"}],
        "primary": "id",
        "searchable": ["body"],
    }]
    db = await open_db(tmp_path, schema=schema)
    try:
        await db.wait(await db.insert("notes", [
            {"id": "n1", "body": "缓存淘汰策略 LRU"},
            {"id": "n2", "body": "终端复用器 tmux"},
        ]))
        before = {r["id"] for r in await db.query(
            "SELECT id FROM notes WHERE search(body, '缓存淘汰')")}
        await db.wait(await db.rebuild())
        after = {r["id"] for r in await db.query(
            "SELECT id FROM notes WHERE search(body, '缓存淘汰')")}
        assert "n1" in before and "n1" in after   # BM25 keyword hit survives rebuild
        assert before == after                     # rebuild repopulated the same projection
    finally:
        await db.close()


async def test_search_respects_time_window(db):
    await _seed(db)
    assert await db.query("SELECT card_id FROM cards WHERE search(issue, 'pty tmux')", ds_end="20990101")
    assert await db.query("SELECT card_id FROM cards WHERE search(issue, 'pty tmux')", ds_end="20000101") == []


async def test_search_needs_a_searchable_column(tmp_path):
    schema = [{"table": "notes",
               "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
               "primary": "id"}]
    db = await open_db(tmp_path, schema=schema, embedder=None)
    try:
        with pytest.raises(QueryError):
            await db.query("SELECT * FROM notes WHERE search(text, 'x')")   # text not searchable
    finally:
        await db.close()
