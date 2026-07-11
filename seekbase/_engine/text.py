"""Tokenization for full-text (BM25) search.

DuckDB's ``fts`` splits on whitespace, which does not segment Chinese (no
spaces). We pre-tokenize with **jieba** (search mode) into a space-joined token
string that both the index (consumer) and the query (executor) share — so the
same segmentation is applied on both sides. English/ASCII words pass through
lowercased; whitespace tokens are dropped.
"""
from __future__ import annotations

_jieba = None


def _cutter():
    global _jieba
    if _jieba is None:
        import jieba  # lazy: importing jieba loads its dictionary (~1s)

        _jieba = jieba
    return _jieba


def tokens(text: str) -> str:
    """Space-joined jieba tokens (search mode), lowercased, blanks dropped.
    Feed the result to DuckDB fts (index side) and match_bm25 (query side)."""
    if not text:
        return ""
    out = []
    for t in _cutter().lcut_for_search(str(text)):
        t = t.strip().lower()
        if t:
            out.append(t)
    return " ".join(out)
