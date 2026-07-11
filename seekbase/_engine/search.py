"""SearchEngine — semantic + full-text search, **inside DuckDB** (no LanceDB).

Single engine: vectors and the inverted index live in the same DuckDB file as
the structured data. Per ``(table, searchable column)`` a derived table
``_sb_search_<table>__<col>(pk, txt, tok, vec FLOAT[dim])`` carries:
  - ``vec`` — the embedding, indexed by the ``vss`` HNSW index (cosine);
  - ``tok`` — jieba-tokenized text, indexed by the ``fts`` BM25 index.

``search(col, 'text')`` fuses the two with **RRF** (reciprocal rank fusion) into
one score per pk. The derived table is a *rebuildable projection* (like the old
LanceDB role): the append-only event log + file mirror stay canonical.

Index maintenance (all off the single-writer bridge):
  - HNSW is **incremental** — upsert = DELETE+INSERT, delete = DELETE; no rebuild.
  - FTS is a **static snapshot** — the consumer calls ``rebuild_fts`` after a
    batch of upserts/deletes so ``search()`` reflects them.

Single-file storage means a **constant, tiny fd count** — this is the whole
reason for collapsing LanceDB into DuckDB (no per-fragment fd growth / EMFILE).
"""
from __future__ import annotations

import inspect

from .._types import EmbedderInvalid
from . import text

# candidate depth pulled from each arm (vss / fts) before RRF
_K = 100


def stab(table: str, col: str) -> str:
    """Derived search table name for a (table, column)."""
    return f"_sb_search_{table}__{col}"


class SearchEngine:
    def __init__(self, bridge, conn, schema, embedder, dim: int) -> None:
        self._bridge = bridge
        self._conn = conn
        self._embedder = embedder
        self._dim = dim
        self._pairs = [(s.name, c) for s in schema.tables for c in s.searchable]

    # ─── setup ─────────────────────────────────────────────────────────
    @classmethod
    async def create(cls, bridge, conn, schema, embedder) -> "SearchEngine":
        dim = int(embedder.dim)
        self = cls(bridge, conn, schema, embedder, dim)
        await bridge.run(self._setup)
        return self

    def _setup(self) -> None:
        for ext in ("vss", "fts"):
            try:
                self._conn.execute(f"INSTALL {ext}; LOAD {ext};")
            except Exception:
                self._conn.execute(f"LOAD {ext};")  # already installed offline
        self._conn.execute("SET hnsw_enable_experimental_persistence=true;")
        for table, col in self._pairs:
            d = stab(table, col)
            self._conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{d}" '
                f"(pk VARCHAR PRIMARY KEY, txt VARCHAR, tok VARCHAR, vec FLOAT[{self._dim}])")
            self._conn.execute(
                f'CREATE INDEX IF NOT EXISTS "{d}_hnsw" ON "{d}" '
                "USING HNSW(vec) WITH (metric='cosine')")
            # ensure the BM25 index (and its fts_main_<d> schema) exists so
            # match_bm25 never references a missing schema, even before data.
            self._conn.execute(f"PRAGMA create_fts_index('{d}', 'pk', 'tok', overwrite=1)")

    # ─── embedding ─────────────────────────────────────────────────────
    async def _embed(self, texts: list[str]) -> list[list[float]]:
        r = self._embedder.embed(texts)
        if inspect.isawaitable(r):
            r = await r
        out = [[float(x) for x in v] for v in r]
        for v in out:
            if len(v) != self._dim:
                raise EmbedderInvalid(f"expected dim {self._dim}, got {len(v)}")
        return out

    # ─── maintenance (consumer-facing) ─────────────────────────────────
    async def upsert(self, table: str, col: str, pk, text_: str) -> None:
        vec = (await self._embed([text_]))[0]
        tok = text.tokens(text_)
        d = stab(table, col)

        def _do():
            self._conn.execute(f'DELETE FROM "{d}" WHERE pk=?', [str(pk)])
            self._conn.execute(f'INSERT INTO "{d}" VALUES (?,?,?,?)', [str(pk), str(text_), tok, vec])

        await self._bridge.run(_do)

    async def delete(self, table: str, col: str, pk) -> None:
        d = stab(table, col)
        await self._bridge.run(
            lambda: self._conn.execute(f'DELETE FROM "{d}" WHERE pk=?', [str(pk)]))

    async def rebuild_fts(self, table: str, col: str) -> None:
        d = stab(table, col)
        await self._bridge.run(
            lambda: self._conn.execute(
                f"PRAGMA create_fts_index('{d}', 'pk', 'tok', overwrite=1)"))

    def reset_inline(self) -> None:
        """Clear every derived table + refresh its (now empty) FTS index.
        Called *inside* an existing bridge block (e.g. rebuild) — no bridge.run,
        so it must run on the bridge thread already."""
        for table, col in self._pairs:
            d = stab(table, col)
            self._conn.execute(f'DELETE FROM "{d}"')
            self._conn.execute(f"PRAGMA create_fts_index('{d}', 'pk', 'tok', overwrite=1)")

    # ─── query: RRF(vss, fts) ──────────────────────────────────────────
    async def hybrid(self, table: str, col: str, text_: str, k: int = _K) -> list[tuple[str, float]]:
        """Fuse cosine-ANN (vss) and BM25 (fts) by reciprocal rank fusion."""
        vec = (await self._embed([text_]))[0]
        tok = text.tokens(text_)
        d = stab(table, col)
        f = f"fts_main_{d}"
        sql = (
            f"WITH v AS (SELECT pk, row_number() OVER (ORDER BY dd) rk FROM "
            f'(SELECT pk, array_cosine_distance(vec, ?::FLOAT[{self._dim}]) dd '
            f'FROM "{d}" WHERE vec IS NOT NULL ORDER BY dd LIMIT {k})), '
            f"ff AS (SELECT pk, row_number() OVER (ORDER BY sc DESC) rk FROM "
            f"(SELECT pk, {f}.match_bm25(pk, ?) sc "
            f'FROM "{d}" WHERE {f}.match_bm25(pk, ?) IS NOT NULL ORDER BY sc DESC LIMIT {k})) '
            f"SELECT COALESCE(v.pk, ff.pk) pk, "
            f"COALESCE(1.0/(60+v.rk),0)+COALESCE(1.0/(60+ff.rk),0) score "
            f"FROM v FULL OUTER JOIN ff ON v.pk=ff.pk ORDER BY score DESC LIMIT {k}")

        def _do():
            return self._conn.execute(sql, [vec, tok, tok]).fetchall()

        rows = await self._bridge.run(_do)
        return [(str(pk), float(sc)) for pk, sc in rows]
