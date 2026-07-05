"""time_machine — as-of 回退 + 只读闸 场景. See README.md."""
from __future__ import annotations

import pytest

from seekbase import ReadOnlyError
from tests.conftest import open_db


async def test_as_of_before_any_write_sees_nothing(tmp_path):
    live = await open_db(tmp_path)
    await live.table("cards").insert({"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
    await live.close()

    past = await open_db(tmp_path, as_of="2000-01-01T00:00:00+00:00")
    try:
        assert await past.table("cards").count() == 0  # nothing existed back then
    finally:
        await past.close()


async def test_as_of_at_row_creation_sees_the_row(tmp_path):
    live = await open_db(tmp_path)
    await live.table("cards").insert({"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
    (row,) = await live.sql("SELECT created_at FROM cards")
    t = row["created_at"]
    await live.close()

    now = await open_db(tmp_path, as_of=t)
    try:
        assert await now.table("cards").count() == 1
    finally:
        await now.close()


async def test_time_machine_connection_is_read_only(tmp_path):
    live = await open_db(tmp_path)
    await live.table("cards").insert({"card_id": "c1", "issue": "x", "kind": "k", "n": 1})
    await live.close()

    past = await open_db(tmp_path, as_of="2100-01-01T00:00:00+00:00")
    try:
        with pytest.raises(ReadOnlyError):
            await past.table("cards").insert(
                {"card_id": "c2", "issue": "y", "kind": "k", "n": 2}
            )
        with pytest.raises(ReadOnlyError):
            await past.table("cards").delete().eq("card_id", "c1")
    finally:
        await past.close()
