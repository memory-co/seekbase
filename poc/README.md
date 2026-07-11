# POC:在 SQL 里直接「模糊检索中文」的两条路

验证两种引擎方案,目标一致——**一条 SQL 直接对中文做模糊检索**(语义向量 + 关键词 BM25 + 二者融合)。给 seekbase 选型用。

| | 方案 A | 方案 B |
|---|---|---|
| 引擎 | DuckDB 原生 `lance` 扩展(DuckLabs × LanceDB, 2026-05) | DuckDB `vss` + `fts` 单引擎 |
| 脚本 | [`a_lance_ext.py`](a_lance_ext.py) | [`b_vss_fts.py`](b_vss_fts.py) |
| 向量检索 | `lance_vector_search()` | `array_distance()` + HNSW 索引 |
| 全文/BM25 | `lance_fts()` | `fts` 扩展 `match_bm25()` |
| hybrid | `lance_hybrid_search()`(内置) | 自己写 RRF(一条 SQL) |

## 怎么跑

```bash
# 确定性 hash embedder(仅验证机械链路,无真语义):
./run.sh
# 真·中文向量(阿里云 DashScope text-embedding-v3, dim 1024):
QWEN_KEY=sk-xxx ./run.sh
```

共享件在 [`_shared.py`](_shared.py):中文语料、embedder(hash / Qwen 二选一)、CJK 分词。
方案 A 的 `.lance` 数据集写到系统临时目录(不污染仓库);方案 B 全内存。

## 实测结论(QWEN_KEY 真向量)

**两条路都成立**——中文的向量检索、BM25、hybrid 都能在 SQL 里跑通。几个关键发现:

1. **中文 BM25 必须先分词**。DuckDB / Lance 的 FTS 都按空白切词,整段中文不切=只能整串精确匹配。POC 用无依赖的 **bigram 分词**(`缓存淘汰`→`缓存 存淘 淘汰`)就能中;生产上换 jieba 更准。这条对两个方案**一样**,是「中文全文检索」的通用前提,不是某个引擎的问题。

2. **两个引擎给出的排序完全一致**。同一份 Qwen 向量喂进去,方案 A/B 的 top-k 一模一样(连同一个「不理想」的命中都一致)——说明**引擎选型 ≠ 检索质量**,质量只取决于 embedder。选型该看的是工程属性(见下),不是准不准。

3. **embedding 那步(文本→向量)两个方案都还在 Python 里做**。`lance` 扩展和 `vss` 都只吃现成向量,不会替你调 embedder——这和之前「为什么 search 不能用 UDF」的结论一致:查询文本先在应用层 embed,再把向量塞进 SQL。

4. **方案 A 的 hybrid 是内置的**(`lance_hybrid_search`,带 `alpha` 调权);方案 B 的 hybrid 要**自己写 RRF**,但一条 CTE 就够,结果也对。

## 两方案的工程权衡(选型要点)

**方案 A(lance 扩展)**
- ➕ 一句 `INSTALL lance` 就位;向量/全文/hybrid **三个函数开箱**;数据以 `.lance` 列存(版本化、随机读快)。
- ➖ **2026-05 刚出**,扩展成熟度/打包(`INSTALL` 联网 vs 离线内置)要落实;数据面是 Lance dataset,和 seekbase 现有 append-only 事件表/时光机视图怎么对齐要设计。

**方案 B(vss + fts 单引擎)**
- ➕ **少一个引擎、少一套进程外依赖**,search 全是原生 SQL,和结构化查询同库 join 最顺;一个 `.db` 文件搞定。
- ➕ **向量(HNSW)是增量的**:实测插入新行不重建就进 top-k 且仍走索引,删除立即生效——和 LanceDB 一样能逐行 upsert/delete,配现有 outbox/consumer 逐行兑现。
- ➖ **只有 `fts` 索引是静态快照**——插入不自动更新,得 `create_fts_index(overwrite=1)` **全量重建**(已验证)。append-only 下靠周期性重建,可挂在 consumer 异步位置,成本随规模涨。
- ➖ **HNSW 落盘两个运维项**:① 落盘库建索引要 `SET hnsw_enable_experimental_persistence=true`(默认报 Binder Error;名字带 "experimental",崩溃恢复耐久性要留意);② 删除留墓碑,长期要 `PRAGMA hnsw_compact_index` 压缩(≠ 全量重建)。

## 对 seekbase 的意义

现在 seekbase 是 DuckDB + LanceDB 双引擎,靠正则抽 `search()` → Python 调 LanceDB → 临时表 → join 缝合。两个方案都能**把这层手工胶水换成 SQL 原生**:

- 走 **A**:`search(col,'中文')` → 应用层 embed → 重写成 `lance_vector_search(...)`,DuckDB 原生 join，省掉临时表。
- 走 **B**:干脆**砍掉 LanceDB**,向量/全文都进 DuckDB,少一个引擎和一套最终一致性;代价是 fts/HNSW 索引偏静态、要重建。

结论:**能不能中文模糊检索——两个都能,且质量等价**;真正的取舍在「A=多一个新扩展但索引增量友好」vs「B=单引擎最简但索引偏静态要重建」。
