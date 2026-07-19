# search — 管道里的检索 source(可插拔引擎:LanceDB / DuckDB-vss)

> 状态:**设计稿(pipeline 方向,未落)**。这一稿把检索从「SQL 里的 `search()` 一等算子」翻成「**管道的一个 source 段**」——`search <表> '文本'` 吃不了输入、产一张 `(pk, _score, …)` 结果表,交给下游 DuckDB `SELECT` 去查(见 [pipeline-as-anything.md](pipeline-as-anything.md))。检索**引擎藏在 source 段背后、可插拔**:LanceDB 或 DuckDB-`vss`+`fts`,产物都只承诺是一张表。
>
> **和现网代码的差异**:现网仍是 `search()` UDF + 单引擎 DuckDB-vss(重写/缝合那套,git 历史里);本文按管道方向重写,`search()` 函数**退休**,检索改由 source 段承担、引擎可换。survives:hybrid RRF(§3)、jieba 分词(§4)整段基本不变——它们是**引擎内部**的活,换外壳不换算法。

## 1. 定位:检索是一个 source 段,不是 SQL 函数

管道模型下(pipeline-as-anything §2),每段吃一张表、吐一张表。检索是**第一段(source)**:无输入、产一张命中表。

```
search cards "pty 终端"                             │ source:embed + 分词 → 引擎 hybrid → _in(card_id, …, _score)
  | SELECT * FROM _in WHERE kind='issue'            │ transform:一整条 DuckDB SQL,吃 _in
        ORDER BY _score DESC LIMIT 20               │   (WHERE/ORDER BY/LIMIT 全是 SQL 自己的活)
```

- **不再有 `search()` 这个函数**。旧法把 `search(列,'文本')` 内嵌进一条 SQL,查询前要**重写 + 缝合**(正则抽占位 → 算候选临时表 → `LEFT JOIN` 缝回可见性视图),还带一串边界情况(注释里的 `search(...)` 误判、参数不能绑定)。这套复杂度**整体退休**——原因见 [pipeline-as-anything.md §3](pipeline-as-anything.md)。
- **search source 的产物是一张真表**,恒名 `_in`,下游 `FROM _in` 即可。搜索参数(`cards`、`'pty 终端'`)是 stage 的**普通参数**,天然可绑定(`search cards ?`)。
- **一条 query 要搜多列/多次** = 管道里多段 source(或分支),各自成表,不抢 `_score_<列>` 命名空间。

## 2. 可插拔引擎:source 段背后的后端契约

`search` 段对下游**只承诺一件事**:产出一张 `(pk, _score, …)` 表。**背后是 LanceDB 还是 DuckDB-vss,管道不关心**——这就是 stage 边界的价值:引擎被关进 source 段里,不再和结构化 SQL 焊在同一条重写链上。

```
                 ┌───────────── search source(接口) ─────────────┐
 "pty 终端" ──→  │  embed + jieba 分词  →  [ 引擎:hybrid RRF ]  │  ──→  _in(pk, _score, …)
                 └───────────────────────────────────────────────┘
                         引擎 ∈ { LanceDB , DuckDB-vss+fts }        ← 可插拔,产物都是表
```

后端契约(两个实现都满足):

| 契约项 | 说明 |
|---|---|
| 输入 | `(表, 查询向量, 查询 token, k, as-of 谓词)` |
| 输出 | `[(pk, score)]`——RRF 融合分,按分降序 |
| 可见性 | as-of 谓词下推进候选(§6),回溯到历史存活集 |
| 派生性 | 后端索引可从 canonical 文件整体重建(见 [store.md](store.md)) |

**只要满足这个契约,任何向量库都能当 search 后端**。下面 §3/§4 是契约内部的算法(两后端共享的 hybrid + 分词),§5 是两个具体后端的存储形态与取舍。

## 3. hybrid:RRF 融合向量与全文(引擎内部,两后端共享)

`search` 段拿到文本后:① embed 成向量、jieba 分词成 token;② 在后端取 vss(向量语义)/ fts(BM25 全文)各自 top-k;③ 用 **RRF(reciprocal rank fusion)** 按名次融合:

```sql
-- 概念形态(DuckDB-vss 后端;LanceDB 后端是等价的 hybrid API 调用):
WITH v AS (SELECT pk, row_number() OVER (ORDER BY d)      rk FROM
              (SELECT <主键> pk, array_cosine_distance(_vec_<列>, $qvec) d
               FROM D WHERE _vec_<列> IS NOT NULL AND <可见性谓词>
               ORDER BY d LIMIT cand)),                                 -- 走 HNSW
     f AS (SELECT pk, row_number() OVER (ORDER BY s DESC)  rk FROM
              (SELECT <主键> pk, match_bm25(<主键>, $qtok, fields := '_tok_<列>') s
               FROM D WHERE match_bm25(<主键>, $qtok, fields := '_tok_<列>') IS NOT NULL
               AND <可见性谓词> ORDER BY s DESC LIMIT cand))             -- 走 BM25(限定该列)
SELECT COALESCE(v.pk, f.pk) pk,
       COALESCE(1.0/(60+v.rk),0) + COALESCE(1.0/(60+f.rk),0) AS score  -- RRF, k0=60
FROM v FULL OUTER JOIN f ON v.pk=f.pk ORDER BY score DESC LIMIT k
```

- **为什么 RRF、不直加分**:cosine 距离和 BM25 分不同量纲,按**名次**融合最稳,不用调权重。k0=60 是常用默认。
- **per-column 限定**:向量按 `_vec_<列>` 各列各自;BM25 用 `fields := '_tok_<列>'` 限到那一列,和向量对齐。
- 融合出的 `(pk, score)` 就是 §2 契约的输出,直接物化成 `_in`——**不再有「缝回外层 SQL」这一步**(那是旧法 UDF 才需要的)。

## 4. 中文分词:jieba(索引侧 / 查询侧同一套)

DuckDB `fts`(以及大多 BM25 实现)按**空白**切词,切不动没有空格的中文。所以 BM25 前先用 **jieba**(search 模式 `lcut_for_search`)把文本切成空格分隔 token:

- **索引侧与查询侧同一套切词**:写入时写 `_tok_<列>`、`search` 段前切查询串,都走 `EmbeddingService` 里同一个 jieba 函数。
- ASCII 词原样小写、空白 token 丢掉;中文按 jieba 词典切。
- `jieba` 纯 Python、无 C 依赖,已进核心依赖。

## 5. 两个后端的存储形态与取舍(引擎可换的代价,诚实讲)

§2 说引擎可插拔;这里摊开两个后端**长什么样、各付什么账**。这正是这一稿相对旧的「单引擎 / 无 LanceDB」叙事**反转**的地方:不再钦定一个引擎,而是把选择权交给场景。

### 5.1 DuckDB-vss 后端:检索列长在业务表上

向量/全文列就长在业务表 `_sb_<表>` 上,和结构化同一个 `duck.db`:

```
_sb_<表>( <主键> PRIMARY KEY, <业务列>…, ds, created_at, deleted_ds, deleted_at,
          _vec_<列> FLOAT[dim],   -- vss/HNSW,每可搜列一个
          _tok_<列> VARCHAR )     -- fts/BM25,一个索引盖住所有 tok 列
```

- 向量在 insert 时**就地 embed、随行写定**,主键写一次 → `_vec`/`_tok` 写定后永不 UPDATE(只软删)。**为什么写定不改**:落盘 HNSW 是 experimental,`UPDATE NULL→向量` 会段错误(已复现);主键写一次从根上避开。
- FTS 是静态快照,每次 insert 同步 `create_fts_index(overwrite=1)` 重建该表(成本随规模涨;写少读多可接受)。
- search source 直接在这张表上跑 §3 的 RRF,产物物化成 `_in`。

### 5.2 LanceDB 后端:独立版本化向量库

向量索引住在一个**独立的 LanceDB store**(不在 duck.db 里),同样从 canonical 文件喂养;search source 调它的 hybrid API 拿 `(pk, score)`,再物化成一张 DuckDB 表 `_in` 交给下游。

### 5.3 取舍(选后端 = 认领它的账)

| | DuckDB-vss 后端 | LanceDB 后端 |
|---|---|---|
| **fd** | 单文件、fd 恒定(就 `duck.db`+WAL)——EMFILE 从结构上消失 | 版本化、每写生成碎片文件、每操作开句柄——**EMFILE 风险回归**,需 compaction/重连机械 |
| **内存** | HNSW 常驻 RAM(天花板 ≈ 向量数×dim×4B) | 列存 + 可 mmap,内存压力小 |
| **段间交换** | 同引擎,产物 temp view 零拷给下游 | 跨引擎,产物要物化成 DuckDB 表(一次拷贝) |
| **写入** | 随行 embed、写定不改;FTS 同步重建 | 版本化 append,天然增量;compaction 是后台活 |
| **何时选** | 写少读多、内存可控、图省事(单文件零运维) | 向量量大、要版本化/列存/独立扩缩、内存吃紧 |

> **立场**:管道**不绑定**任何一个引擎——`search` source 是接口,两个后端都是它的实现,产物都是表。`duck-vss` 适合默认(单文件、零运维);上量、内存吃紧、要版本化时切 `lance`,**代价就是那张表里的 fd 账**(它当初被收掉正是因为在 memory.talk 里反复撞 `Too many open files`)。选谁是**场景决策**,不再是全局钦定。

## 6. as-of 下推:search 尊重时光机

`search` source 吃一个 as-of 谓词(来自管道的 `@asof` / `scan` 入参,见 [time_machine.md](time_machine.md)),下推进 §3 的候选子句:

- **不倒带**(as-of now):`deleted_ds IS NULL`。
- **倒带到 D**:`ds <= D AND (deleted_ds IS NULL OR deleted_ds > D)`——软删的行仍在索引里(软删不重建索引),回溯到它还活着的那天照样被搜到。查和搜对齐,不再有「搜不到历史」的不对称。
- **over-fetch**:HNSW/BM25 是「先取 top-k、再按谓词过滤」,历史查询里被排除的行会挤占名额、导致少返回。历史路径把候选池 `cand` 放大 `_OVERFETCH`×(实测 ×2 补回召回)。

## 7. 写入同步语义

不管哪个后端,`search` 段读到的都是**已落库、可搜**的状态:向量在 insert 时就地 embed、随行/随版本落库,FTS 同步重建(duck-vss)或版本化 append(lance)。`insert` 返回即可搜(read-your-write),没有异步兑现窗口。写路径细节见 [store.md](store.md) / [concurrency.md](concurrency.md)。

## 8. 与其他文档

- [pipeline-as-anything.md](pipeline-as-anything.md):检索为什么是 source 段、`search()` UDF 为何退休、`_in` 表怎么交给下游。
- [store.md](store.md):两层存储、检索后端如何从 canonical 文件重建。
- [schema.md](schema.md):`searchable` 如何接线到可插拔检索后端。
- [time_machine.md](time_machine.md):as-of 谓词,作为 source 段的入参下推进候选。
- [api/query.md](../api/query.md):管道 / 检索的对外用法。
