"""schema — 声明式 SCHEMA 校验 + 早失败 场景. See README.md."""
from __future__ import annotations

import pytest

from seekbase import EmbedderInvalid, NotSupportedYet, QueryError, SchemaError
from seekbase.schema import parse_schema
from tests.conftest import open_db

# ────────── parse_schema validation ──────────


def test_requires_exactly_one_primary_key():
    with pytest.raises(SchemaError):
        parse_schema({"t": {"columns": {"a": "str"}}})              # none
    with pytest.raises(SchemaError):
        parse_schema({"t": {"columns": {"a": "str primary", "b": "str primary"}}})


def test_reserved_metadata_columns_cannot_be_declared():
    with pytest.raises(SchemaError):
        parse_schema({"t": {"columns": {"id": "str primary", "created_at": "str"}}})
    with pytest.raises(SchemaError):
        parse_schema({"t": {"columns": {"id": "str primary", "deleted_at": "str"}}})


def test_column_type_must_be_known():
    with pytest.raises(SchemaError):
        parse_schema({"t": {"columns": {"id": "str primary", "x": "blob"}}})


def test_files_placeholder_must_be_a_real_column():
    with pytest.raises(SchemaError):
        parse_schema(
            {"t": {"columns": {"id": "str primary"}, "files": "x/{missing}.json"}}
        )


def test_valid_schema_parses():
    schema = parse_schema(
        {"cards": {"columns": {"card_id": "str primary", "issue": "str"},
                   "searchable": ["issue"], "files": "cards/{card_id}.json"}}
    )
    spec = schema.table("cards")
    assert spec.primary_key == "card_id"
    assert spec.searchable == ("issue",)
    assert spec.files.mode == "json"


# ────────── runtime rejection ──────────


async def test_unknown_column_is_rejected(db):
    with pytest.raises(QueryError):
        await db.table("cards").eq("nope", 1)
    with pytest.raises(QueryError):
        await db.table("cards").insert({"card_id": "c1", "bogus": 1})


async def test_searchable_schema_requires_an_embedder(tmp_path):
    """The canonical schema declares a searchable column; opening it without an
    embedder fails at open, not on first search."""
    with pytest.raises(EmbedderInvalid):
        await open_db(tmp_path, embedder=None)


async def test_search_operator_is_accepted_but_deferred(db):
    """The chain is stable now; execution lands with the vector engine (M3)."""
    with pytest.raises(NotSupportedYet):
        await db.table("cards").search("pty tmux").eq("kind", "issue")
