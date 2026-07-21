"""search — 语义检索(管道的 search 源段:`search <表> '文本' | SELECT … FROM _in`)。
See README.md."""
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
        "search cards 'pty tmux terminal' | SELECT card_id, _score FROM _in ORDER BY _score DESC")
    ids = [h["card_id"] for h in hits]
    assert ids[0] == "c3"                 # closest
    assert ids[-1] == "c2"                # redis is least relevant
    assert [h["_score"] for h in hits] == sorted((h["_score"] for h in hits), reverse=True)


async def test_search_combines_with_structured_filter(db):
    await _seed(db)
    hits = await db.query(
        "search cards 'cache redis' "
        "| SELECT card_id FROM _in WHERE kind = 'design' ORDER BY _score DESC LIMIT 1")
    assert hits == [{"card_id": "c2"}]


async def test_per_column_search_is_independent(tmp_path):
    """Each searchable column has its own vector index: the same query text
    against different columns (``--col``) can rank differently."""
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
            "search docs 'tmux terminal panes' --col title "
            "| SELECT id FROM _in ORDER BY _score DESC LIMIT 1"))[0]
        top_body = (await db.query(
            "search docs 'tmux terminal panes' --col body "
            "| SELECT id FROM _in ORDER BY _score DESC LIMIT 1"))[0]
        assert top_title["id"] == "d1"    # title match
        assert top_body["id"] == "d2"     # body match — same text, different column, different row
    finally:
        await db.close()


async def test_multi_column_table_requires_col(tmp_path):
    """A table with several searchable columns needs an explicit ``--col``;
    per-column scores come from one pipeline per column (the old multi-search
    single-SQL form is retired with the ``search()`` UDF)."""
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
        with pytest.raises(QueryError):   # ambiguous column → explicit --col required
            await db.query("search docs 'tmux' | SELECT id FROM _in")

        by_title = {r["id"]: r["_score"] for r in await db.query(
            "search docs 'tmux terminal panes' --col title | SELECT id, _score FROM _in")}
        by_body = {r["id"]: r["_score"] for r in await db.query(
            "search docs 'tmux terminal panes' --col body | SELECT id, _score FROM _in")}
        # d1 matches on title, d2 on body — the matching column carries the higher score
        assert by_title["d1"] > by_body.get("d1", 0.0)
        assert by_body["d2"] > by_title.get("d2", 0.0)
    finally:
        await db.close()


async def test_chinese_hybrid_search(tmp_path):
    """中文:search = vss(向量语义)+ fts(BM25 关键词,jieba 分词)RRF 融合。
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
            "search notes '缓存淘汰' | SELECT id, _score FROM _in ORDER BY _score DESC")
        assert hits[0]["id"] == "n2"          # '缓存淘汰' 经 jieba→BM25 命中 n2
        assert all(h["_score"] is not None for h in hits)
    finally:
        await db.close()


async def test_deleted_row_is_not_searchable(db):
    await _seed(db)
    await db.wait(await db.delete("cards", where="card_id = ?", params=["c3"]))
    hits = await db.query(
        "search cards 'pty tmux terminal' | SELECT card_id FROM _in ORDER BY _score DESC")
    assert "c3" not in [h["card_id"] for h in hits]


async def test_rebuild_repopulates_search(tmp_path):
    """rebuild() clears the derived vss+fts projection and replays the file
    mirror; the consumer must repopulate it so search works again."""
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
            "search notes '缓存淘汰' | SELECT id FROM _in")}
        await db.wait(await db.rebuild())
        after = {r["id"] for r in await db.query(
            "search notes '缓存淘汰' | SELECT id FROM _in")}
        assert "n1" in before and "n1" in after   # BM25 keyword hit survives rebuild
        assert before == after                     # rebuild repopulated the same projection
    finally:
        await db.close()


async def test_search_respects_time_window(db):
    await _seed(db)
    q = "search cards 'pty tmux' | SELECT card_id FROM _in"
    assert await db.query(q, ds_end="20990101")
    assert await db.query(q, ds_end="20000101") == []


async def test_search_time_travels_over_deleted_rows(tmp_path, monkeypatch):
    """The search source shares the structured read's as-of predicate: a row
    deleted *now* is still searchable when the query time-travels to before its
    deletion (and a not-yet-created row stays invisible). The vss/fts index
    keeps soft-deleted rows; only the ds/deleted horizon filters them."""
    import seekbase.service.write_service as ws

    def _freeze(ds):
        monkeypatch.setattr(ws, "today", lambda: ds)
        monkeypatch.setattr(ws, "now", lambda: ds + "T12:00:00+00:00")

    db = await open_db(tmp_path, schema=[{
        "table": "notes",
        "columns": [{"name": "id", "type": "str"}, {"name": "body", "type": "str"}],
        "primary": "id", "searchable": ["body"]}])
    try:
        _freeze("20260102")
        await db.wait(await db.insert("notes", {"id": "n1", "body": "缓存淘汰策略 LRU 与 LFU"}))
        _freeze("20260105")
        await db.wait(await db.delete("notes", where="id = ?", params=["n1"]))

        q = "search notes '缓存淘汰' | SELECT id FROM _in"
        # as-of now: deleted, not searchable
        assert await db.query(q) == []
        # as-of day03 (created@02, deleted@05): alive → searchable
        assert [r["id"] for r in await db.query(q, ds_end="20260103")] == ["n1"]
        # as-of day01 (before creation): invisible
        assert await db.query(q, ds_end="20260101") == []
    finally:
        await db.close()


async def test_search_needs_a_searchable_column(tmp_path):
    schema = [{"table": "notes",
               "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
               "primary": "id"}]
    db = await open_db(tmp_path, schema=schema, embedder=None)
    try:
        with pytest.raises(QueryError):      # no searchable column on the table
            await db.query("search notes 'x' | SELECT * FROM _in")
        with pytest.raises(QueryError):      # explicit --col that is not searchable
            await db.query("search notes 'x' --col text | SELECT * FROM _in")
    finally:
        await db.close()
