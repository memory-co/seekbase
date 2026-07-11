"""方案 A:DuckDB 原生 `lance` 扩展(DuckLabs × LanceDB, 2026-05),纯中文。

验证:用 lance 扩展把向量检索/全文/hybrid 下推进 Lance 层,在 SQL 里直接
模糊检索中文。步骤:COPY 建 .lance 数据集 → lance_vector_search / lance_fts /
lance_hybrid_search。fts / hybrid 若需先建 Lance 索引,则记录为发现。

跑:  python poc/a_lance_ext.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import duckdb

from _shared import CORPUS, FTS_QUERIES, QUERIES, cjk_tokens_str, get_embedder


def main() -> None:
    print("== 方案 A:duckdb `lance` 扩展(中文) ==")
    emb = get_embedder()
    dim = emb.dim

    con = duckdb.connect()
    con.execute("INSTALL lance; LOAD lance;")

    tmp = Path(tempfile.mkdtemp(prefix="poc_lance_"))
    ds = str(tmp / "docs.lance")

    # 先在内存表里备好数据,再 COPY 成 lance 数据集
    con.execute(f"CREATE TABLE src(pk VARCHAR, body VARCHAR, tokens VARCHAR, vec FLOAT[{dim}]);")
    vecs = emb.embed([t for _, t in CORPUS])
    for (pk, body), v in zip(CORPUS, vecs):
        con.execute("INSERT INTO src VALUES (?,?,?,?)", [pk, body, cjk_tokens_str(body), v])
    con.execute(f"COPY (SELECT * FROM src) TO '{ds}' (FORMAT lance, mode 'overwrite');")
    print(f"  lance 数据集: {ds}")

    ok = True

    # ── ① 向量检索:lance_vector_search ──────────────────────────
    print("\n-- ① 语义检索(lance_vector_search) --")
    for q, expect, note in QUERIES:
        qv = emb.embed([q])[0]
        lit = "[" + ",".join(repr(x) for x in qv) + f"]::FLOAT[{dim}]"
        try:
            rows = con.execute(
                f"SELECT pk, _distance FROM lance_vector_search('{ds}','vec',{lit}, k=2)"
            ).fetchall()
            got = {r[0] for r in rows}
            hit = got == expect
            ok &= hit
            print(f"  [{'OK ' if hit else 'MISS'}] {q!r:16} top2={got}  期望={expect}  ({note})")
        except Exception as e:
            ok = False
            print(f"  [ERR] {q!r}: {type(e).__name__}: {str(e)[:140]}")

    # ── ② 全文:lance_fts(可能需先建 Lance FTS 索引) ────────────
    print("\n-- ② 关键词检索(lance_fts) --")
    for q, expect in FTS_QUERIES:
        qt = cjk_tokens_str(q)
        try:
            rows = con.execute(
                "SELECT pk FROM lance_fts(?, 'tokens', ?, k=3)", [ds, qt]).fetchall()
            got = {r[0] for r in rows}
            hit = expect <= got
            print(f"  [{'OK ' if hit else 'MISS'}] {q!r:8}(→{qt!r}) 命中={got}  期望⊇{expect}")
        except Exception as e:
            print(f"  [ERR] {q!r}: {type(e).__name__}: {str(e)[:140]}")

    # ── ③ hybrid:lance_hybrid_search ────────────────────────────
    print("\n-- ③ hybrid(lance_hybrid_search) --")
    q = "缓存怎么过期淘汰"
    qv = emb.embed([q])[0]
    qt = cjk_tokens_str(q)
    lit = "[" + ",".join(repr(x) for x in qv) + f"]::FLOAT[{dim}]"
    try:
        rows = con.execute(
            f"SELECT pk, _hybrid_score FROM lance_hybrid_search("
            f"'{ds}','vec',{lit},'tokens',?, alpha=0.5, k=3) ORDER BY _hybrid_score DESC",
            [qt]).fetchall()
        print(f"  查询 {q!r}: {rows}")
    except Exception as e:
        print(f"  [ERR] {type(e).__name__}: {str(e)[:180]}")

    print(f"\n向量检索:{'通过 ✅' if ok else 'MISS/ERR(见上)⚠️'}")


if __name__ == "__main__":
    main()
