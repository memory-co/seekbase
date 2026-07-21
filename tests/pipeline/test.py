"""pipeline — SPL 式管道:切分、SQL 缺省、算子降级、参数分配、位置推导。
See README.md."""
from __future__ import annotations

import pytest

from seekbase import QueryError
from seekbase.operator import Cap, Operator, Registry, builtin_operators
from seekbase.service.pipeline_service import split_pipeline


async def _seed(db):
    await db.wait(await db.insert("cards", [
        {"card_id": "c1", "issue": "pty tmux terminal", "kind": "issue", "n": 1},
        {"card_id": "c2", "issue": "redis cache design", "kind": "design", "n": 2},
        {"card_id": "c3", "issue": "tmux panes ERROR log", "kind": "issue", "n": 3},
    ]))


# ─── 切分:| 只在顶层切;|| 与字符串字面量里的 | 不切 ───────────────────

def test_split_on_top_level_pipe():
    assert split_pipeline("a | b | c") == ["a", "b", "c"]


def test_double_pipe_is_sql_concat():
    assert split_pipeline("SELECT 'a' || 'b'") == ["SELECT 'a' || 'b'"]


def test_pipe_inside_string_literal_is_kept():
    assert split_pipeline("grep 'a|b' --field x | SELECT 1") == \
        ["grep 'a|b' --field x", "SELECT 1"]


def test_empty_segment_rejected():
    with pytest.raises(QueryError):
        split_pipeline("scan cards | | SELECT 1")


# ─── SQL 是缺省:首 token 不命中 registry → 整段是 SQL ──────────────────

async def test_pure_sql_zero_pipes_is_not_a_pipeline(db):
    await _seed(db)
    rows = await db.query("WITH t AS (SELECT * FROM cards) SELECT count(*) AS c FROM t")
    assert rows == [{"c": 3}]


async def test_unknown_leading_token_is_sql_error_not_unknown_operator(db):
    # "frobnicate" 不是算子 → 整段当 SQL → DuckDB 语法错(QueryError),
    # 而不是「未知算子」这种专门错误。
    with pytest.raises(QueryError):
        await db.query("frobnicate the database")


# ─── 降级:多段融合成一条 WITH;grep 翻成 WHERE ─────────────────────────

async def test_three_stage_pipeline_fuses(db):
    await _seed(db)
    rows = await db.query(
        "search cards 'tmux terminal' | grep 'ERROR' --field issue "
        "| SELECT card_id FROM _in")
    assert [r["card_id"] for r in rows] == ["c3"]


async def test_scan_source_feeds_sql(db):
    await _seed(db)
    rows = await db.query(
        "scan cards | SELECT kind, count(*) AS c FROM _in GROUP BY kind ORDER BY kind")
    assert rows == [{"kind": "design", "c": 1}, {"kind": "issue", "c": 2}]


async def test_sql_can_lead_a_pipeline(db):
    await _seed(db)
    rows = await db.query(
        "SELECT card_id, n FROM cards WHERE n >= 2 | SELECT count(*) AS c FROM _in")
    assert rows == [{"c": 2}]


# ─── 位置从签名推导:source 只能打头,中段不能打头 ──────────────────────

async def test_source_must_start_the_pipeline(db):
    await _seed(db)
    with pytest.raises(QueryError):
        await db.query("scan cards | search cards 'x' | SELECT 1")


async def test_middle_operator_cannot_start(db):
    with pytest.raises(QueryError):
        await db.query("grep 'x' --field issue | SELECT 1")


# ─── 参数按段分配 ───────────────────────────────────────────────────────

async def test_params_flow_into_sql_segments(db):
    await _seed(db)
    rows = await db.query(
        "scan cards | SELECT card_id FROM _in WHERE n >= ? AND kind = ? ORDER BY card_id",
        params=[1, "issue"])
    assert [r["card_id"] for r in rows] == ["c1", "c3"]


async def test_leftover_params_rejected(db):
    with pytest.raises(QueryError):
        await db.query("scan cards | SELECT card_id FROM _in", params=["extra"])


# ─── 读写守卫穿过管道 ──────────────────────────────────────────────────

async def test_pipeline_stays_read_only(db):
    await _seed(db)
    with pytest.raises(QueryError):
        await db.query("scan cards | DELETE FROM _sb_cards")
    (c,) = await db.query("SELECT count(*) AS c FROM cards")
    assert c["c"] == 3


# ─── registry:命名守卫 + 不覆盖 ───────────────────────────────────────

def test_registry_rejects_sql_leading_keyword():
    class Bad(Operator):
        name = "select"
        caps = frozenset({Cap.PURE})
        def optimize_duck(self, args):
            return "SELECT 1", []
    r = Registry()
    with pytest.raises(QueryError):
        r.register(Bad())


def test_registry_rejects_duplicate_name():
    r = Registry()
    for op in builtin_operators():
        r.register(op)
    with pytest.raises(QueryError):
        r.register(builtin_operators()[0])       # search again → explicit error


def test_registry_rejects_operator_without_any_duck_cell():
    class Empty(Operator):
        name = "empty"
    r = Registry()
    with pytest.raises(QueryError):
        r.register(Empty())


# ─── bash runtime:切段 + JSONL 桥(需 sandboxed 策略)────────────────────

async def _sandboxed_db(tmp_path):
    from seekbase import Policy, Seekbase
    from tests.conftest import SCHEMA, FakeEmbedder
    return await Seekbase.open(tmp_path / "db", schema=SCHEMA, embedder=FakeEmbedder(),
                               policy=Policy(mode="sandboxed"))


async def test_duck_bash_duck_phases(tmp_path):
    db = await _sandboxed_db(tmp_path)
    try:
        await _seed(db)
        rows = await db.query(
            "scan cards | sh 'grep tmux' | SELECT count(*) AS c FROM _in")
        assert rows == [{"c": 2}]                     # c1/c3 contain tmux
    finally:
        await db.close()


async def test_bash_final_phase(tmp_path):
    db = await _sandboxed_db(tmp_path)
    try:
        await _seed(db)
        rows = await db.query("scan cards | sh 'grep redis'")
        assert [r["card_id"] for r in rows] == ["c2"]
    finally:
        await db.close()


async def test_fused_bash_run_is_one_chain(tmp_path):
    db = await _sandboxed_db(tmp_path)
    try:
        await _seed(db)
        rows = await db.query(
            "scan cards | sh 'grep tmux' | sh 'head -1' | SELECT card_id FROM _in")
        assert len(rows) == 1                          # 两个相邻 bash 段融成一条进程链
    finally:
        await db.close()


async def test_bash_cannot_start_bounded_query(tmp_path):
    db = await _sandboxed_db(tmp_path)
    try:
        with pytest.raises(QueryError):
            await db.query("sh 'echo hi' | SELECT 1")
    finally:
        await db.close()


async def test_bash_failure_surfaces(tmp_path):
    db = await _sandboxed_db(tmp_path)
    try:
        await _seed(db)
        with pytest.raises(QueryError):
            await db.query("scan cards | sh 'exit 3'")
    finally:
        await db.close()
