"""EmbeddingService — the embedding provider subdomain (text → vectors + tokens).

Wraps the injected ``Embedder`` (the API-vs-local switch lives in ``embedders/``:
``ApiEmbedder`` today, local sentence-transformers is a TODO) and adds the
Chinese tokenization (jieba) that BM25 needs. No DuckDB here — it just turns
text into the ``(vec, tok)`` pairs the store persists / queries.
"""
from __future__ import annotations

import inspect

from .._types import EmbedderInvalid

_jieba = None


def _tokens(s: str) -> str:
    """Space-joined jieba tokens (search mode), lowercased, blanks dropped —
    the same segmentation on the index side (``_tok`` column) and the query
    side, so DuckDB fts (which splits on whitespace) can match Chinese."""
    global _jieba
    if not s:
        return ""
    if _jieba is None:
        import jieba  # lazy: importing jieba loads its dictionary (~1s)

        _jieba = jieba
    out = [t.strip().lower() for t in _jieba.lcut_for_search(str(s))]
    return " ".join(t for t in out if t)


class EmbeddingService:
    def __init__(self, embedder) -> None:
        self._embedder = embedder
        self._dim = int(embedder.dim)

    @property
    def dim(self) -> int:
        return self._dim

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
        return _tokens(s)

    async def embed_records(self, spec, records: list[dict]) -> tuple[dict, dict]:
        """Inline embed + tokenize each searchable column across ``records``.
        Returns (vecs, toks): col -> list aligned with records (None where the
        column's value is empty). Used by the write / rebuild paths."""
        vecs: dict[str, list] = {}
        toks: dict[str, list] = {}
        for col in spec.searchable:
            texts = [rec.get(col) for rec in records]
            idx = [i for i, t in enumerate(texts) if t is not None and str(t) != ""]
            emb = await self.embed([str(texts[i]) for i in idx]) if idx else []
            cv: list = [None] * len(records)
            ct: list = [None] * len(records)
            for j, i in enumerate(idx):
                cv[i] = emb[j]
                ct[i] = self.tok(str(texts[i]))
            vecs[col], toks[col] = cv, ct
        return vecs, toks
