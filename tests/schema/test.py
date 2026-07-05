"""schema — 声明式 SCHEMA 校验 + 早失败 场景. See README.md."""
from __future__ import annotations

import pytest

from seekbase import EmbedderInvalid, QueryError, SchemaError
from seekbase.schema import parse_schema
from tests.conftest import open_db

# ────────── parse_schema validation (list form) ──────────


def _t(**kw):
    base = {"table": "t", "columns": [{"name": "id", "type": "str"}], "primary": "id"}
    base.update(kw)
    return [base]


def test_schema_must_be_a_list():
    with pytest.raises(SchemaError):
        parse_schema({"t": {"columns": []}})       # old dict form rejected


def test_requires_primary_pointing_at_a_column():
    with pytest.raises(SchemaError):
        parse_schema(_t(primary="nope"))
    with pytest.raises(SchemaError):
        parse_schema([{"table": "t", "columns": [{"name": "id", "type": "str"}]}])  # missing


def test_primary_must_be_str_or_int():
    with pytest.raises(SchemaError):
        parse_schema(_t(columns=[{"name": "id", "type": "float"}], primary="id"))


def test_reserved_metadata_columns_cannot_be_declared():
    for meta in ("ds", "created_at", "deleted_ds", "deleted_at"):
        with pytest.raises(SchemaError):
            parse_schema(_t(columns=[{"name": "id", "type": "str"}, {"name": meta, "type": "str"}]))


def test_column_type_must_be_known():
    with pytest.raises(SchemaError):
        parse_schema(_t(columns=[{"name": "id", "type": "blob"}], primary="id"))


def test_decimal_precision_scale_validated():
    parse_schema(_t(columns=[{"name": "id", "type": "str"}, {"name": "amt", "type": "decimal(18,2)"}]))
    with pytest.raises(SchemaError):
        parse_schema(_t(columns=[{"name": "id", "type": "str"}, {"name": "amt", "type": "decimal(2,5)"}]))


def test_searchable_must_be_str_column():
    with pytest.raises(SchemaError):
        parse_schema(_t(columns=[{"name": "id", "type": "str"}, {"name": "k", "type": "int"}],
                        searchable=["k"]))


def test_duplicate_table_and_column_rejected():
    with pytest.raises(SchemaError):
        parse_schema([_t()[0], _t()[0]])            # duplicate table 't'
    with pytest.raises(SchemaError):
        parse_schema(_t(columns=[{"name": "id", "type": "str"}, {"name": "id", "type": "str"}]))


def test_valid_schema_with_advanced_types_parses():
    schema = parse_schema([{
        "table": "cards",
        "columns": [{"name": "card_id", "type": "str"}, {"name": "amt", "type": "decimal(18,2)"},
                    {"name": "ts", "type": "timestamptz"}, {"name": "meta", "type": "json"}],
        "primary": "card_id",
    }])
    spec = schema.table("cards")
    assert spec.primary_key == "card_id"
    assert [c.name for c in spec.columns] == ["card_id", "amt", "ts", "meta"]  # order kept


# ────────── advanced types round-trip through DDL ──────────


async def test_advanced_type_columns_open_and_insert(tmp_path):
    schema = [{
        "table": "t",
        "columns": [{"name": "id", "type": "str"}, {"name": "amt", "type": "decimal(18,2)"},
                    {"name": "ts", "type": "timestamptz"}, {"name": "meta", "type": "json"}],
        "primary": "id",
    }]
    db = await open_db(tmp_path, schema=schema, embedder=None)
    try:
        await db.wait(await db.insert("t", {
            "id": "a", "amt": "12.34", "ts": "2026-07-05T12:00:00+00:00",
            "meta": {"k": [1, 2, 3]},
        }))
        (row,) = await db.query("SELECT id, meta FROM t")
        assert row["id"] == "a"
    finally:
        await db.close()


# ────────── runtime rejection ──────────


async def test_unknown_column_in_insert_rejected(db):
    with pytest.raises(QueryError):
        await db.insert("cards", {"card_id": "c1", "bogus": 1})


async def test_searchable_schema_requires_embedder(tmp_path):
    with pytest.raises(EmbedderInvalid):
        await open_db(tmp_path, embedder=None)      # canonical schema has a searchable col
