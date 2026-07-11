"""方案 B:DuckDB 单引擎 —— vss(向量/HNSW)+ fts(BM25 全文),纯中文。

验证:在同一条 SQL 里做中文的 ① 语义检索(vss)② 关键词检索(fts/BM25)
③ 二者融合(RRF hybrid)。全部在 DuckDB 内,不依赖 LanceDB。

跑:  python poc/b_vss_fts.py
"""
from __future__ import annotations

import duckdb

from _shared import (CORPUS, FTS_QUERIES, QUERIES, cjk_tokens_str, get_embedder)


def main() -> None:
    print("== 方案 B:duckdb vss + fts(中文) ==")
    emb = get_embedder()
    dim = emb.dim

    con = duckdb.connect()  # 内存库
    con.execute("INSTALL vss; LOAD vss;")
    con.execute("INSTALL fts; LOAD fts;")

    # 建表:pk / 原文 body / 分词后 tokens(给 FTS) / 向量 vec(给 vss)
    con.execute(f"CREATE TABLE docs(pk VARCHAR, body VARCHAR, tokens VARCHAR, vec FLOAT[{dim}]);")
    vecs = emb.embed([t for _, t in CORPUS])
    for (pk, body), v in zip(CORPUS, vecs):
        con.execute("INSERT INTO docs VALUES (?,?,?,?)",
                    [pk, body, cjk_tokens_str(body), v])

    # 索引:HNSW(向量)+ BM25(全文)
    con.execute("CREATE INDEX hnsw_idx ON docs USING HNSW(vec) WITH (metric = 'cosine');")
    con.execute("PRAGMA create_fts_index('docs', 'pk', 'tokens');")

    # ── ① 语义检索:array_distance 走 HNSW ────────────────────────
    print("\n-- ① 语义检索(vss / 向量) --")
    ok = True
    for q, expect, note in QUERIES:
        qv = emb.embed([q])[0]
        rows = con.execute(
            "SELECT pk, array_distance(vec, ?::FLOAT[" + str(dim) + "]) d "
            "FROM docs ORDER BY d LIMIT 2", [qv]).fetchall()
        got = {r[0] for r in rows}
        hit = got == expect
        ok &= hit
        print(f"  [{'OK ' if hit else 'MISS'}] {q!r:16} top2={got}  期望={expect}  ({note})")

    # ── ② 关键词检索:BM25(中文需先分词) ───────────────────────
    print("\n-- ② 关键词检索(fts / BM25,中文 bigram 分词) --")
    for q, expect in FTS_QUERIES:
        qt = cjk_tokens_str(q)
        rows = con.execute(
            "SELECT pk, fts_main_docs.match_bm25(pk, ?) s FROM docs "
            "WHERE s IS NOT NULL ORDER BY s DESC LIMIT 3", [qt]).fetchall()
        got = {r[0] for r in rows}
        hit = expect <= got
        ok &= hit
        print(f"  [{'OK ' if hit else 'MISS'}] {q!r:8}(→{qt!r}) 命中={got}  期望⊇{expect}")

    # ── ③ hybrid:向量 + BM25 用 RRF 融合(纯 SQL) ───────────────
    print("\n-- ③ hybrid 融合(RRF, 一条 SQL) --")
    q = "缓存怎么过期淘汰"
    qv = emb.embed([q])[0]
    qt = cjk_tokens_str(q)
    rows = con.execute(f"""
        WITH v AS (
            SELECT pk, row_number() OVER (ORDER BY array_distance(vec, ?::FLOAT[{dim}])) rk
            FROM docs),
        f AS (
            SELECT pk, row_number() OVER (ORDER BY fts_main_docs.match_bm25(pk, ?) DESC) rk
            FROM docs WHERE fts_main_docs.match_bm25(pk, ?) IS NOT NULL)
        SELECT d.pk, d.body,
               COALESCE(1.0/(60+v.rk),0) + COALESCE(1.0/(60+f.rk),0) AS rrf
        FROM docs d LEFT JOIN v USING(pk) LEFT JOIN f USING(pk)
        ORDER BY rrf DESC LIMIT 3
    """, [qv, qt, qt]).fetchall()
    print(f"  查询 {q!r}:")
    for pk, body, rrf in rows:
        print(f"    {pk}  rrf={rrf:.4f}  {body}")

    print(f"\n结果:{'全部通过 ✅' if ok else '有 MISS(hash embedder 无真语义时正常)⚠️'}")


if __name__ == "__main__":
    main()
