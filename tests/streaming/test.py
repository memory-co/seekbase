"""streaming — 常驻无界管道(pipeline-streaming.md):watch 跟文件、微批落库、
at-least-once + 幂等 sink、checkpoint 重启不重灌、无界 source 进有界 query 编译期拒。
See README.md."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from seekbase import QueryError
from tests.conftest import open_db


def _line(pk, text, n):
    return json.dumps({"card_id": pk, "issue": text, "kind": "log", "n": n}) + "\n"


async def _wait_count(db, want, timeout=5.0):
    """Poll until the table holds ``want`` rows (streams land asynchronously)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        (r,) = await db.query("SELECT count(*) AS c FROM cards")
        if r["c"] == want:
            return
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"expected {want} rows, has {r['c']}")
        await asyncio.sleep(0.05)


async def test_watch_ingest_lands_and_dedupes(tmp_path, db):
    logs = tmp_path / "logs"
    logs.mkdir()
    f = logs / "a.jsonl"
    f.write_text(_line("s1", "first streamed line", 1))

    h = await db.stream(f"watch '{logs}/*.jsonl' | ingest cards --flush-ms 40", name="t")
    try:
        await _wait_count(db, 1)
        # append two lines — one new, one replaying an existing pk (must skip)
        with open(f, "a") as fh:
            fh.write(_line("s2", "second line", 2))
            fh.write(_line("s1", "dup replay must be skipped", 99))
        await _wait_count(db, 2)
        rows = await db.query("SELECT card_id, n FROM cards ORDER BY card_id")
        assert rows == [{"card_id": "s1", "n": 1}, {"card_id": "s2", "n": 2}]  # n=99 skipped
    finally:
        await h.stop()
    assert h.exception() is None
    assert not h.running


async def test_streamed_rows_are_searchable(tmp_path, db):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "a.jsonl").write_text(_line("s1", "streamed pty terminal issue", 1))
    h = await db.stream(f"watch '{logs}/*.jsonl' | ingest cards --flush-ms 40", name="t")
    try:
        await _wait_count(db, 1)
    finally:
        await h.stop()
    hits = await db.query("search cards 'pty terminal' | SELECT card_id FROM _in LIMIT 1")
    assert hits == [{"card_id": "s1"}]


async def test_checkpoint_survives_restart(tmp_path, db):
    logs = tmp_path / "logs"
    logs.mkdir()
    f = logs / "a.jsonl"
    f.write_text(_line("s1", "one", 1))
    pipeline = f"watch '{logs}/*.jsonl' | ingest cards --flush-ms 40"

    h = await db.stream(pipeline, name="t")
    await _wait_count(db, 1)
    await h.stop()

    # restart the same stream name: checkpointed offsets → nothing re-read;
    # new lines appended while stopped are picked up.
    with open(f, "a") as fh:
        fh.write(_line("s2", "two", 2))
    h = await db.stream(pipeline, name="t")
    try:
        await _wait_count(db, 2)
    finally:
        await h.stop()
    rows = await db.query("SELECT card_id FROM cards ORDER BY card_id")
    assert [r["card_id"] for r in rows] == ["s1", "s2"]


async def test_half_written_line_waits_for_newline(tmp_path, db):
    logs = tmp_path / "logs"
    logs.mkdir()
    f = logs / "a.jsonl"
    f.write_text('{"card_id":"s1","issue":"partial')          # no newline yet
    h = await db.stream(f"watch '{logs}/*.jsonl' | ingest cards --flush-ms 40", name="t")
    try:
        await asyncio.sleep(0.3)
        (r,) = await db.query("SELECT count(*) AS c FROM cards")
        assert r["c"] == 0                                    # tail line not consumed
        with open(f, "a") as fh:
            fh.write('","kind":"log","n":1}\n')               # complete it
        await _wait_count(db, 1)
    finally:
        await h.stop()


async def test_unbounded_source_rejected_in_query(db):
    with pytest.raises(QueryError):                           # watch → bounded query: 编译期拒
        await db.query("watch 'x.jsonl' | SELECT count(*) FROM _in")
    with pytest.raises(QueryError):                           # ingest 是流 sink
        await db.query("scan cards | ingest cards")


async def test_stream_shape_validated(db, tmp_path):
    with pytest.raises(QueryError):                           # 必须以 ingest 收尾
        await db.stream("watch 'x.jsonl' | scan cards", name="bad1")
    with pytest.raises(QueryError):                           # 源必须无界
        await db.stream("scan cards | ingest cards", name="bad2")
    with pytest.raises(QueryError):                           # 流里不放 SQL 段
        await db.stream("watch 'x.jsonl' | SELECT 1 | ingest cards", name="bad3")


async def test_duplicate_stream_name_rejected(tmp_path, db):
    logs = tmp_path / "logs"
    logs.mkdir()
    h = await db.stream(f"watch '{logs}/*.jsonl' | ingest cards", name="t")
    try:
        with pytest.raises(QueryError):
            await db.stream(f"watch '{logs}/*.jsonl' | ingest cards", name="t")
    finally:
        await h.stop()


@pytest.mark.skipif(__import__("shutil").which("jq") is None, reason="jq not installed")
async def test_bash_middle_reshapes(tmp_path):
    """中段 bash 链(EXEC)要 sandboxed 策略:jq 把原始行整形成表结构再落库。"""
    from seekbase import Policy, Seekbase
    from tests.conftest import SCHEMA, FakeEmbedder
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "raw.jsonl").write_text(
        '{"id":"r1","msg":"pty broke","level":"error","noise":"x"}\n'
        '{"id":"r2","msg":"all fine","level":"info","noise":"y"}\n')
    db = await Seekbase.open(tmp_path / "db", schema=SCHEMA, embedder=FakeEmbedder(),
                             policy=Policy(mode="sandboxed"))
    try:
        h = await db.stream(
            f"watch '{logs}/*.jsonl' "
            "| jq 'select(.level==\"error\") | {card_id:.id, issue:.msg, kind:.level, n:1}' "
            "| ingest cards --flush-ms 40", name="reshape")
        try:
            await _wait_count(db, 1)
        finally:
            await h.stop()
        rows = await db.query("SELECT card_id, issue, kind FROM cards")
        assert rows == [{"card_id": "r1", "issue": "pty broke", "kind": "error"}]
    finally:
        await db.close()
