"""SearchEngine — vss + fts **on the business tables** (single-table model).

No separate vector store, no derived projection: each business table
``_sb_<table>`` carries, per searchable column, a ``_vec_<col> FLOAT[dim]``
(indexed by a vss/HNSW cosine index) and a ``_tok_<col>`` (jieba tokens; one
BM25/fts index covers all tok columns, per-column matching via
``match_bm25(…, fields := '_tok_<col>')``).

Vectors are written **inline at INSERT** and never updated: primary keys are
write-once (re-insert is rejected), so a row's vector is set once and only ever
soft-deleted. This sidesteps the DuckDB crash where UPDATE-ing a NULL→vector on
an experimental on-disk HNSW index segfaults.

``search(col, 'text')`` fuses vss (cosine ANN) + fts (BM25) by RRF (k0=60).
"""
from __future__ import annotations

import inspect

from .._types import EmbedderInvalid
from . import text

_K = 100


def veccol(col: str) -> str:
    return f"_vec_{col}"


def tokcol(col: str) -> str:
    return f"_tok_{col}"


class SearchEngine:
    def __init__(self, bridge, conn, schema, embedder, dim: int) -> None:
        self._bridge = bridge
        self._conn = conn
        self._schema = schema
        self._embedder = embedder
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    # ─── setup ─────────────────────────────────────────────────────────
    @classmethod
    async def create(cls, bridge, conn, schema, embedder) -> "SearchEngine":
        self = cls(bridge, conn, schema, embedder, int(embedder.dim))
        await bridge.run(self._setup)
        return self

    def _setup(self) -> None:
        for ext in ("vss", "fts"):
            try:
                self._conn.execute(f"INSTALL {ext}; LOAD {ext};")
            except Exception:
                self._conn.execute(f"LOAD {ext};")   # already installed offline
        self._conn.execute("SET hnsw_enable_experimental_persistence=true;")
        for spec in self._schema.tables:
            if not spec.searchable:
                continue
            phys = f"_sb_{spec.name}"
            for col in spec.searchable:
                self._conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "{phys}_{col}_hnsw" ON "{phys}" '
                    f"USING HNSW(\"{veccol(col)}\") WITH (metric='cosine')")
            self._build_fts(spec)

    def _build_fts(self, spec) -> None:
        phys = f"_sb_{spec.name}"
        cols = ", ".join(f"'{tokcol(c)}'" for c in spec.searchable)
        self._conn.execute(
            f"PRAGMA create_fts_index('{phys}', '{spec.primary_key}', {cols}, overwrite=1)")

    def rebuild_fts_inline(self, table: str) -> None:
        """Rebuild a table's BM25 index. Called inside an existing bridge block
        (insert/rebuild), so it uses the connection directly (no bridge.run)."""
        spec = self._schema.table(table)
        if spec.searchable:
            self._build_fts(spec)

    # ─── embedding / tokenizing ────────────────────────────────────────
    async def embed(self, texts: list[str]) -> list[list[float]]:
        r = self._embedder.embed(texts)
        if inspect.isawaitable(r):
            r = await r
        out = [[float(x) for x in v] for v in r]
        for v in out:
            if len(v) != self._dim:
                raise EmbedderInvalid(f"expected dim {self._dim}, got {len(v)}")
        return out

    def tok(self, s: str) -> str:
        return text.tokens(s)

    # ─── query: RRF(vss, fts) directly on the business table ───────────
    async def hybrid(self, table: str, col: str, text_: str, k: int = _K) -> list[tuple[str, float]]:
        vec = (await self.embed([text_]))[0]
        tok = text.tokens(text_)
        spec = self._schema.table(table)
        phys = f"_sb_{table}"
        pk = spec.primary_key
        f = f"fts_main_{phys}"
        vc, tc = veccol(col), tokcol(col)
        sql = (
            f'WITH v AS (SELECT pk, row_number() OVER (ORDER BY dd) rk FROM '
            f'(SELECT "{pk}" pk, array_cosine_distance("{vc}", ?::FLOAT[{self._dim}]) dd '
            f'FROM "{phys}" WHERE "{vc}" IS NOT NULL AND "deleted_ds" IS NULL '
            f'ORDER BY dd LIMIT {k})), '
            f"ff AS (SELECT pk, row_number() OVER (ORDER BY sc DESC) rk FROM "
            f"(SELECT \"{pk}\" pk, {f}.match_bm25(\"{pk}\", ?, fields := '{tc}') sc "
            f'FROM "{phys}" WHERE {f}.match_bm25("{pk}", ?, fields := \'{tc}\') IS NOT NULL '
            f'AND "deleted_ds" IS NULL ORDER BY sc DESC LIMIT {k})) '
            f"SELECT COALESCE(v.pk, ff.pk) pk, "
            f"COALESCE(1.0/(60+v.rk),0)+COALESCE(1.0/(60+ff.rk),0) score "
            f"FROM v FULL OUTER JOIN ff ON v.pk=ff.pk ORDER BY score DESC LIMIT {k}")

        def _do():
            return self._conn.execute(sql, [vec, tok, tok]).fetchall()

        rows = await self._bridge.run(_do)
        return [(str(pk_), float(sc)) for pk_, sc in rows]
