# search — 语义检索(SQL 里的 `search()` 函数)

## 这个场景在测什么

`search(列, '文本')` 是 SQL 里的一等算子:指定搜哪一列(每个可搜列各自一个向量索引),自动 embed + 向量检索(LanceDB),暴露
`_score_<列>` 列(单个 search 也附便捷别名 `_score`),和结构化过滤 / 时间窗写在同一条 `query` SQL 里。写入的向量由后台
consumer 从 outbox 异步兑现,`wait(ticket)` 排干。

1. **按相似度排序**:`search()` 返回带 `_score` 的行,越像越靠前。
2. **和结构化过滤组合**:`WHERE search(列, '…') AND kind='…'` 同一条 SQL。
3. **每列独立**:同一 query 文本搜不同列可得不同结果;多个 `search()` 各暴露 `_score_<列>`(各列各自向量索引)。
4. **删除后搜不到**:打墓碑的行不再被 `search()` 带出。
5. **和时间窗组合**:`ds_end` 早于写入日 → 搜不到(分区裁剪也套在 search 上)。
6. **对非 `searchable` 列用 `search()`** → `QueryError`(早失败)。

## 不在这测什么

- 结构化读写走 [`read_write/`](../read_write/) / 文件镜像走 [`file_mirror/`](../file_mirror/)
- 真 embedder(网络)—— 用确定性 `FakeEmbedder`(bag-of-chars,有排序信号、零依赖)

## fixture 来源

- `db`(`tests/conftest.py`)—— `cards`(`issue` 可搜)+ `FakeEmbedder`
- `open_db`(自定义无 searchable 的 schema)—— 测「search 需要 searchable 表」
