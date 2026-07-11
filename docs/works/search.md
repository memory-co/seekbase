# search — 语义 + 全文 hybrid 检索(单引擎 vss + fts)

> 状态:**M5 已落**。`search(列, '文本')` 是 SQL 里的一等算子,在 **DuckDB 单引擎** 内做 hybrid 检索:向量语义(`vss`/HNSW)+ 全文 BM25(`fts`)用 **RRF** 融合。本文讲这一层的设计:派生表、两种索引的维护语义、中文分词、RRF 融合、以及为什么把 LanceDB 收进 DuckDB。对外用法见 [api/query.md](../api/query.md#search--sql-里的语义检索)。

## 1. 定位:检索是 DuckDB 里的一层派生投影

每个 `searchable` 列有一张**派生表**,和事件表、文件镜像一样从 canonical 文件可重建:

```
_sb_search_<表>__<列>(
    pk   VARCHAR PRIMARY KEY,   -- 主键(和事件表 / 文件里的行对齐)
    txt  VARCHAR,               -- 原文
    tok  VARCHAR,               -- jieba 分词后的空格串(喂 fts)
    vec  FLOAT[dim]             -- embedding(喂 vss)
)
  ├─ vss:CREATE INDEX … USING HNSW(vec) WITH (metric='cosine')
  └─ fts:PRAGMA create_fts_index(…, 'pk', 'tok')   → BM25
```

- **每列一张、各自索引**:`search(列, …)` 只搜那一列;一条 query 可有多个 `search()`(搜不同列),各自 `_score_<列>`。
- **可重建投影**:它就是原来 **LanceDB 扮演的角色**,现在搬进同一个 `duck.db`。因为是派生、非 canonical 历史,可以增删(`upsert`/`delete`),不受事件表 insert-only 约束(canonical 在文件 + 事件表,§[store.md](store.md))。

## 2. 两种索引,两种维护语义

| | vss(向量 / HNSW) | fts(全文 / BM25) |
|---|---|---|
| 写入反映 | **增量**:upsert = `DELETE`+`INSERT`、delete = `DELETE`,索引即时更新、**不重建** | **静态快照**:`INSERT`/`DELETE` 不自动进索引 |
| 兑现方式 | consumer 逐条写 vec 行 | consumer 每批处理完 `create_fts_index(…, overwrite=1)` **重建被触及的 (表,列)** |
| 时机 | —— | **在标 outbox done 之前重建**,故 `wait(ticket)→done` 即意味 `search()` 搜得到 |

- 这是走 consumer 异步路径的原因之一:FTS 是静态的,写入要靠**周期性重建**;向量虽增量,但 embed 要走网络,一并异步。
- **落盘 HNSW 要开实验开关**:落盘库建 HNSW 需 `SET hnsw_enable_experimental_persistence=true`(seekbase 在 open 时设);名字带 "experimental",崩溃恢复耐久性要留意。
- **删除留墓碑**:HNSW 删除是打标记,长期要 `PRAGMA hnsw_compact_index` 压缩(维护动作,≠ 全量重建;列 DESIGN §10)。

## 3. hybrid:RRF 融合 vss 与 fts

`search(列, '文本')` 出现时,executor:① 把文本 embed 成向量、jieba 分词成 token;② 在该列派生表上分别取 vss / fts 的 top-k;③ 用 **RRF(reciprocal rank fusion)** 融合:

```sql
-- 概念形态(实际在 _engine/search.py):
WITH v AS (SELECT pk, row_number() OVER (ORDER BY d)      rk FROM
              (SELECT pk, array_cosine_distance(vec, $qvec) d
               FROM D WHERE vec IS NOT NULL ORDER BY d LIMIT k)),      -- 走 HNSW
     f AS (SELECT pk, row_number() OVER (ORDER BY s DESC)  rk FROM
              (SELECT pk, F.match_bm25(pk, $qtok) s
               FROM D WHERE F.match_bm25(pk,$qtok) IS NOT NULL ORDER BY s DESC LIMIT k))  -- 走 BM25
SELECT COALESCE(v.pk, f.pk) pk,
       COALESCE(1.0/(60+v.rk),0) + COALESCE(1.0/(60+f.rk),0) AS score  -- RRF, k0=60
FROM v FULL OUTER JOIN f ON v.pk=f.pk ORDER BY score DESC LIMIT k
```

- **为什么 RRF、不直加分**:cosine 距离和 BM25 分不同量纲,按**名次**融合最稳,不用调权重。k0=60 是常用默认。
- 融合出的 `(pk, score)` 灌进临时表,`LEFT JOIN` 进主查询的**重放视图**(§[time_machine.md](time_machine.md))——结构化谓词、`ds` 时间窗、排序都在同一条外层 SQL 里;`score` 暴露成 `_score_<列>`(单个 search 另附 `_score`,见 [api/query.md](../api/query.md))。
- **时光机**:vss/fts 返回**当前态**候选,外层 join 重放视图(as-of `ds_start`/`ds_end`)再裁掉当时不存在 / 已删的行——语义与旧 LanceDB 版一致。

## 4. 中文分词:jieba

DuckDB 的 `fts` 按**空白**切词,切不动没有空格的中文。所以 BM25 前先用 **jieba**(search 模式 `lcut_for_search`)把文本切成空格分隔的 token:

- **索引侧与查询侧同一套切词**:consumer 建 `tok` 列、`search()` 前切查询串,都走 `_engine/text.tokens()`。
- ASCII 词原样小写、空白 token 丢掉;中文按 jieba 词典切。
- `jieba` 是纯 Python、无 C 依赖,已进核心依赖(见 [../../DESIGN.md](../../DESIGN.md) §2)。

## 5. 为什么把 LanceDB 收进 DuckDB(单引擎动因)

LanceDB 是**版本化、每写生成碎片文件**的存储,配上每次操作开 table 句柄,在 memory.talk 里反复撞 `Too many open files (os error 24)`——要靠不停 compaction + 关连接重连来放 fd,背了一整套 EMFILE 恢复机械(探测 → compact 全表 → `reset_connection` → 重试)+ 30 分钟一轮的主动 compaction + fd 上限监控。

**DuckDB 单文件让打开的 fd 数恒定**(就 `duck.db` + WAL),这类 fd 耗尽从结构上消失,上面那套机械整个删掉;检索和结构化同库 join 也最顺,search 全是原生 SQL。

**代价(诚实讲)**:约束从 fd 转到**内存**(HNSW 常驻 RAM,天花板按 向量数×dim×4B 估)+ **FTS 周期重建**(成本随规模涨)+ HNSW 落盘仍标 experimental。对内存可控的规模,这是划算的交换。

## 6. 与其他文档

- [store.md](store.md):两层存储、检索派生表在 rebuild / 一致性里的位置。
- [schema.md](schema.md):`searchable` 如何接线 vss+fts 派生表。
- [time_machine.md](time_machine.md):search 结果如何 join 重放视图做 as-of 裁剪。
- [api/query.md](../api/query.md):`search()` 的对外用法、`_score_<列>`、多列 search。
