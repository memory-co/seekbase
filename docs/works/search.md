# search — 语义 + 全文 hybrid 检索(单引擎 vss + fts)

> 状态:**已落**。`search(列, '文本')` 是 SQL 里的一等算子,在 **DuckDB 单引擎** 内做 hybrid 检索:向量语义(`vss`/HNSW)+ 全文 BM25(`fts`)用 **RRF** 融合。本文讲这一层的设计:检索列如何长在业务表上、两种索引的维护语义、中文分词、RRF 融合、以及为什么把 LanceDB 收进 DuckDB。对外用法见 [api/query.md](../api/query.md#search--sql-里的语义检索)。

## 1. 定位:检索列就长在业务表上

没有单独的向量库、也没有派生检索表。每个 `searchable` 列在**业务表本身** `_sb_<表>` 上多两列 + 各自索引:

```
_sb_<表>(
    <主键> ... PRIMARY KEY,       -- 业务主键(写一次)
    <业务列> ...,
    ds, created_at, deleted_ds, deleted_at,
    _vec_<列>  FLOAT[dim],         -- 该可搜列的 embedding(喂 vss)
    _tok_<列>  VARCHAR             -- 该可搜列的 jieba 分词空格串(喂 fts)
)
  ├─ vss:CREATE INDEX … USING HNSW(_vec_<列>) WITH (metric='cosine')   -- 每可搜列一个
  └─ fts:PRAGMA create_fts_index(_sb_<表>, <主键>, '_tok_<列1>', '_tok_<列2>', …)  -- 一个 BM25 索引盖住所有 tok 列
```

- **一行一主键**:检索列和业务数据同在一行,天然按主键对齐,不用再维护一张表、不用 join 对齐。
- **每列各自向量索引**:`search(列, …)` 只搜那一列;一条 query 可有多个 `search()`(搜不同列),各自 `_score_<列>`。
- **一个 BM25 索引盖住所有 tok 列**:DuckDB 一张表只能有一个 fts 索引,per-column 命中靠 `match_bm25(pk, q, fields := '_tok_<列>')` 限定字段(§4)。
- 这就是原来 **LanceDB 扮演的角色**,现在收进业务表本身、同一个 `duck.db`。整张表可从 canonical 文件重建(§[store.md](store.md))。

## 2. 两种索引,两种维护语义(写入同步)

向量在 **insert 时就地 embed、随行写入**,主键写一次 → 那一行的 `_vec`/`_tok` **写定后永不 UPDATE**(只可能软删):

| | vss(向量 / HNSW) | fts(全文 / BM25) |
|---|---|---|
| 写入 | insert 时 `_vec_<列>` 随行 `INSERT`,**一次写定、不再改** | insert 后同步 `create_fts_index(overwrite=1)` **重建该表** |
| 删除 | 软删只 `UPDATE deleted_ds`(非索引列),行/向量不动 | 同上;删的行由 `deleted_ds IS NULL` 谓词在查询侧裁掉 |
| 兑现 | **同步**:`insert` 返回即向量已落库、可搜 | **同步**:同一 `insert` 调用里重建,返回即可搜 |

- **为什么向量写定不改(而不是先写 NULL 后填)**:DuckDB 落盘 HNSW 是 experimental,**`UPDATE` 一个 `NULL`→向量会段错误**(已复现)。主键写一次 → 向量在 `INSERT` 时就定、之后只软删(动非索引列),从根上避开这个崩溃。详见 §6。
- **落盘 HNSW 要开实验开关**:`SET hnsw_enable_experimental_persistence=true`(seekbase 在 open 时设);名字带 "experimental",崩溃恢复耐久性要留意。
- **FTS 是静态快照**:`INSERT` 不自动进 BM25 索引,故每次 insert 同步 `overwrite=1` 重建那张表的 fts 索引(成本随表规模涨;写少读多的 memory 场景可接受)。

## 3. 从 `search(列,'文本')` 到 DuckDB 认识的 SQL(重写 + 缝合)

`search(列, '文本')` **不是 DuckDB 的函数**——它是 seekbase 的语法糖。query 前要把它**重写成合法 DuckDB SQL**,再把向量/全文检索结果缝回去。三步:抽取占位 → 算候选 → LEFT JOIN 缝回。整条链路的外层**始终是一条 DuckDB 原生 SQL**(所以 join / 聚合 / 窗口 / `ds` 时间窗照常),向量和全文是旁路算好、以 `(pk, score)` 缝进来。

### 3.1 抽取 + 占位符替换(`extract_searches`)

扫出每个 `search(列, '字面量')`,**原地替换成一个布尔占位** `(_score_<列> IS NOT NULL)`,并记下 `(列, 文本, 分数列名)`。用户写的:

```sql
SELECT card_id, _score FROM cards
WHERE search(issue, 'pty 终端') AND kind = 'issue' ORDER BY _score DESC
```

被重写成引用一个**尚不存在的** `_score_issue` 列的普通 SQL:

```sql
SELECT card_id, _score FROM cards
WHERE (_score_issue IS NOT NULL) AND kind = 'issue' ORDER BY _score DESC
```

`search()` 的语义(「只留语义/关键词命中的行」)就落成了 `_score_<列> IS NOT NULL` 这个 DuckDB 完全认得的谓词。多个 `search()` 各自一个 `_score_<列>`,同名自动加后缀去重。

### 3.2 定表(`search_target`)

`_score_<列>` 该挂到哪张表?按「`列` 是谁的 `searchable` 列」+「该表名是否出现在这条 SQL 里」解析出**唯一**目标表;歧义或找不到 → `QueryError`(早失败)。

### 3.3 算候选 + 缝回(临时表 + LEFT JOIN 可见性视图)

executor 对每个抽出的 search:embed 文本 + jieba 分词 → 在该表的检索列上跑 §4 的 **RRF**,得到 `[(pk, 融合分)]`。duck 把它灌进一张临时表 `_sb_s_<i>(pk, score)`,再在目标表的**可见性视图**上 `LEFT JOIN` 它,把 `score` 暴露成 `_score_<列>`(单 search 另附 `_score` 别名):

```sql
CREATE OR REPLACE TEMP VIEW cards AS
SELECT base.*, _s0.score AS "_score_issue", _s0.score AS "_score"
FROM ( …可见性视图:_sb_cards 上 as-of 的存活行,ds 过滤(见 time_machine.md)… ) base
LEFT JOIN _sb_s_0 _s0 ON CAST(base.card_id AS VARCHAR) = _s0.pk
```

于是重写后的外层 SQL 一切自洽:

- `WHERE (_score_issue IS NOT NULL)` —— LEFT JOIN 后**只有命中的 pk 有分**,`IS NOT NULL` 精确裁到命中集;
- `SELECT _score` / `ORDER BY _score` —— 直接拿融合分投影 / 排序;
- 结构化谓词(`kind='issue'`)、`ds` 时间窗、join/聚合 —— 都在这条外层 SQL 里和检索结果**一起算**。

> **一句话**:`search()` 被拆成「一个布尔占位符(进 `WHERE`)+ 一张分数临时表(`LEFT JOIN` 进可见性视图)」;真正的向量/全文检索(§4 RRF)在旁路完成,以 `(pk, score)` 缝回,外层永远是原生 DuckDB SQL。

> **实现说明**:§3.1 的抽取当前用**正则**,有已知边界情况(SQL 注释里的 `search(...)` 会被误判;`search(列, ?)` 参数绑定暂不支持,只收字面量)。更稳的做法是换 DuckDB 自带 parser(`json_serialize_sql` 走 AST),列为后续项(DESIGN §10)。

## 4. hybrid:RRF 融合 vss 与 fts

`search(列, '文本')` 出现时,executor:① 把文本 embed 成向量、jieba 分词成 token;② 直接在业务表 `_sb_<表>` 的 `_vec_<列>`/`_tok_<列>` 上分别取 vss / fts 的 top-k;③ 用 **RRF(reciprocal rank fusion)** 融合:

```sql
-- 概念形态(实际在 service/store_service.py 的 StoreService.hybrid;D = _sb_<表>,F = fts_main_<D>):
WITH v AS (SELECT pk, row_number() OVER (ORDER BY d)      rk FROM
              (SELECT <主键> pk, array_cosine_distance(_vec_<列>, $qvec) d
               FROM D WHERE _vec_<列> IS NOT NULL AND deleted_ds IS NULL
               ORDER BY d LIMIT k)),                                    -- 走 HNSW
     f AS (SELECT pk, row_number() OVER (ORDER BY s DESC)  rk FROM
              (SELECT <主键> pk, F.match_bm25(<主键>, $qtok, fields := '_tok_<列>') s
               FROM D WHERE F.match_bm25(<主键>, $qtok, fields := '_tok_<列>') IS NOT NULL
               AND deleted_ds IS NULL ORDER BY s DESC LIMIT k))         -- 走 BM25(限定该列)
SELECT COALESCE(v.pk, f.pk) pk,
       COALESCE(1.0/(60+v.rk),0) + COALESCE(1.0/(60+f.rk),0) AS score  -- RRF, k0=60
FROM v FULL OUTER JOIN f ON v.pk=f.pk ORDER BY score DESC LIMIT k
```

- **per-column BM25**:一张表只有一个 fts 索引,`fields := '_tok_<列>'` 把 BM25 限定到那一列,和 vss 的 per-列向量对齐。
- **为什么 RRF、不直加分**:cosine 距离和 BM25 分不同量纲,按**名次**融合最稳,不用调权重。k0=60 是常用默认。
- **软删的行**:两支候选都带 `deleted_ds IS NULL`,已软删的行不进候选。
- 融合出的 `(pk, score)` 如何缝回外层 SQL、暴露成 `_score_<列>`,见 §3.3;外层可见性视图再叠加 as-of `ds_start`/`ds_end` 的时间窗裁剪。

## 5. 中文分词:jieba

DuckDB 的 `fts` 按**空白**切词,切不动没有空格的中文。所以 BM25 前先用 **jieba**(search 模式 `lcut_for_search`)把文本切成空格分隔的 token:

- **索引侧与查询侧同一套切词**:insert 时写 `_tok_<列>`、`search()` 前切查询串,都走 `EmbeddingService`(service/embedding_service.py)里同一个 jieba 分词函数。
- ASCII 词原样小写、空白 token 丢掉;中文按 jieba 词典切。
- `jieba` 是纯 Python、无 C 依赖,已进核心依赖(见 [../../DESIGN.md](../../DESIGN.md) §2)。

## 6. 为什么把 LanceDB 收进 DuckDB(单引擎动因)

LanceDB 是**版本化、每写生成碎片文件**的存储,配上每次操作开 table 句柄,在 memory.talk 里反复撞 `Too many open files (os error 24)`——要靠不停 compaction + 关连接重连来放 fd,背了一整套 EMFILE 恢复机械(探测 → compact 全表 → `reset_connection` → 重试)+ 30 分钟一轮的主动 compaction + fd 上限监控。

**DuckDB 单文件让打开的 fd 数恒定**(就 `duck.db` + WAL),这类 fd 耗尽从结构上消失,上面那套机械整个删掉;检索和结构化同库 join 也最顺,search 全是原生 SQL。

**代价(诚实讲)**:约束从 fd 转到**内存**(HNSW 常驻 RAM,天花板按 向量数×dim×4B 估)+ **FTS 每次 insert 同步重建**(成本随表规模涨)+ HNSW 落盘仍标 experimental。对内存可控、写少读多的 memory 场景,这是划算的交换。

**实验性 HNSW 还顺带定了写入形态**:它 `UPDATE NULL→向量` 会段错误(§2),所以设计成**主键写一次 + insert 内联 embed**——向量随行一次写定、永不 UPDATE(只软删非索引列),既绕开崩溃,也让写入同步、`insert` 返回即可搜。

## 7. 与其他文档

- [store.md](store.md):两层存储、检索列在 rebuild / 一致性里的位置。
- [schema.md](schema.md):`searchable` 如何在业务表上接线 `_vec`/`_tok` 列 + vss/fts 索引。
- [time_machine.md](time_machine.md):search 结果如何 join 可见性视图做 as-of 裁剪。
- [api/query.md](../api/query.md):`search()` 的对外用法、`_score_<列>`、多列 search。
