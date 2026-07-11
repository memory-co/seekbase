# search — hybrid 检索(SQL 里的 `search()` 函数)

## 这个场景在测什么

`search(列, '文本')` 是 SQL 里的一等算子:指定搜哪一列(每个可搜列各自一套 vss+fts),自动 embed + jieba 分词,**hybrid 检索**——向量语义(`vss`/HNSW)+ BM25 全文(`fts`)用 **RRF** 融合,暴露
`_score_<列>` 列(单个 search 也附便捷别名 `_score`),和结构化过滤 / 时间窗写在同一条 `query` SQL 里,全在 **DuckDB 单引擎**内(无 LanceDB)。向量 / 全文就地长在业务表上,
insert 时同步 embed + jieba 落库(向量随行、FTS 同步重建),`insert` 返回即可搜。

1. **按相关度排序**:`search()` 返回带 `_score`(RRF 融合分)的行,越相关越靠前。
2. **和结构化过滤组合**:`WHERE search(列, '…') AND kind='…'` 同一条 SQL。
3. **每列独立**:同一 query 文本搜不同列可得不同结果;多个 `search()` 各暴露 `_score_<列>`(各列各自 vss+fts)。
4. **中文 hybrid**:jieba 分词让 BM25 命中中文关键词(`test_chinese_hybrid_search`),和向量 RRF 融合。
5. **删除后搜不到**:打墓碑的行不再被 `search()` 带出。
6. **rebuild 后仍可搜**:`rebuild()` 清空并从文件重灌(重新 embed + 重建索引),`search()` 照常(`test_rebuild_repopulates_search`)。
7. **和时间窗组合**:`ds_end` 早于写入日 → 搜不到(分区裁剪也套在 search 上)。
8. **对非 `searchable` 列用 `search()`** → `QueryError`(早失败)。

## 不在这测什么

- 结构化读写走 [`read_write/`](../read_write/) / 文件镜像走 [`file_mirror/`](../file_mirror/)
- 真 embedder(网络)—— 用确定性 `FakeEmbedder`(bag-of-chars,有排序信号、零依赖);中文用例靠 jieba+BM25 命中,不依赖 embedder 语义质量

## fixture 来源

- `db`(`tests/conftest.py`)—— `cards`(`issue` 可搜)+ `FakeEmbedder`
- `open_db`(自定义无 searchable 的 schema)—— 测「search 需要 searchable 表」
