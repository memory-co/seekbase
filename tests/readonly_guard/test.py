"""readonly_guard — query 只读、写类 SQL 一律被拒 场景. See README.md."""
from __future__ import annotations

import contextlib

import pytest

from seekbase import QueryError, ReadOnlyError

# every one of these tries to write / mutate through `query` and must be rejected
BANNED = [
    "INSERT INTO cards VALUES ('x','y','z',1)",
    "UPDATE cards SET n = 0",
    "DELETE FROM cards",
    "DROP TABLE _sb_cards",
    "CREATE TABLE evil (x INT)",
    "ALTER TABLE _sb_cards ADD COLUMN x INT",
    "ATTACH ':memory:' AS m",
    "SET memory_limit = '1GB'",
    "CALL pragma_version()",
    "COPY _sb_cards TO '/tmp/leak.csv'",
    # first token is WITH but the statement is DML — the bypass a naive
    # "starts with SELECT/WITH" check would miss (regression: once really deleted)
    "WITH x AS (SELECT 1) DELETE FROM _sb_cards",
    "WITH x AS (SELECT 1) INSERT INTO _sb_cards VALUES ('a','b','c',1)",
    "WITH x AS (SELECT 1) UPDATE _sb_cards SET n = 0",
    # multiple statements smuggling a write after a read
    "SELECT 1; DROP TABLE _sb_cards",
    "SELECT 1; DELETE FROM _sb_cards",
]


@pytest.mark.parametrize("sql", BANNED)
async def test_write_sql_is_rejected(db, sql):
    with pytest.raises(ReadOnlyError):
        await db.query(sql)


async def test_data_survives_every_banned_attempt(db):
    await db.wait(await db.insert("cards", [
        {"card_id": "c1", "issue": "x", "kind": "k", "n": 1},
        {"card_id": "c2", "issue": "y", "kind": "k", "n": 2},
    ]))
    for sql in BANNED:
        with contextlib.suppress(ReadOnlyError, QueryError):
            await db.query(sql)
    (c,) = await db.query("SELECT count(*) AS c FROM cards")
    assert c["c"] == 2                      # nothing got written / dropped


async def test_legit_reads_still_work(db):
    await db.wait(await db.insert("cards", {"card_id": "c1", "issue": "x", "kind": "k", "n": 1}))
    # CTE + subquery + aggregate — all valid reads
    assert await db.query("WITH t AS (SELECT * FROM cards) SELECT card_id FROM t") == [{"card_id": "c1"}]
    assert (await db.query("SELECT count(*) AS c FROM (SELECT * FROM cards)"))[0]["c"] == 1


async def test_delete_where_rejects_second_statement(db):
    with pytest.raises(QueryError):
        await db.delete("cards", where="1=1; DROP TABLE _sb_cards")


async def test_banned_over_http(pair):
    _, client = pair
    with pytest.raises(ReadOnlyError):
        await client.query("WITH x AS (SELECT 1) DELETE FROM _sb_cards")
    with pytest.raises(ReadOnlyError):
        await client.query("DROP TABLE _sb_cards")
