"""embedder_live — 真实 embedding API 端到端验证. See README.md.

默认 skip;设 QWEN_KEY + SEEKBASE_EMBED_URL 才跑。配置全从环境变量读,
key/endpoint 不进代码。
"""
from __future__ import annotations

import os

import pytest

from seekbase import Seekbase

pytestmark = pytest.mark.skipif(
    not (os.getenv("QWEN_KEY") and os.getenv("SEEKBASE_EMBED_URL")),
    reason="set QWEN_KEY + SEEKBASE_EMBED_URL (+ optional SEEKBASE_EMBED_MODEL/DIM) to run",
)

SCHEMA = [
    {"table": "cards",
     "columns": [{"name": "id", "type": "str"}, {"name": "issue", "type": "str"}],
     "primary": "id",
     "searchable": ["issue"]},
]


def _embedder():
    from seekbase.embedders import ApiEmbedder
    return ApiEmbedder(
        base_url=os.environ["SEEKBASE_EMBED_URL"],
        api_key=os.environ["QWEN_KEY"],
        model=os.getenv("SEEKBASE_EMBED_MODEL", "text-embedding-v4"),
        dim=int(os.getenv("SEEKBASE_EMBED_DIM", "1024")),
    )


async def test_live_embedder_semantic_search(tmp_path):
    emb = _embedder()
    db = await Seekbase.open(tmp_path / "db", schema=SCHEMA, embedder=emb)
    try:
        # write; the outbox consumer embeds via the real API, then LanceDB
        st = await db.wait(await db.insert("cards", [
            {"id": "c1", "issue": "how to use tmux terminal multiplexer sessions"},
            {"id": "c2", "issue": "redis vs a local in-process cache for hot keys"},
            {"id": "c3", "issue": "debugging a segfault in C with gdb backtraces"},
        ]))
        assert st["state"] == "done"

        # a query semantically close to c1 (never lexically identical)
        hits = await db.query(
            "SELECT id, _score FROM cards "
            "WHERE search(issue, 'splitting my terminal into panes and windows') "
            "ORDER BY _score DESC LIMIT 3")
        assert hits, "real embedder returned no hits"
        assert hits[0]["id"] == "c1", f"expected c1 top, got {[h['id'] for h in hits]}"
        # scores are real cosine-derived, descending
        assert [h["_score"] for h in hits] == sorted((h["_score"] for h in hits), reverse=True)
    finally:
        await db.close()
        await emb.aclose()
