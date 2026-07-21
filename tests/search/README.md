# search — hybrid 检索(管道的 `search` 源段)

## 这个场景在测什么

`search <表> '<文本>' [--col <列>] [--k <n>]` 是管道的一个 **source 段**:自动
embed + jieba 分词,**hybrid 检索**——向量语义(`vss`/HNSW)+ BM25 全文(`fts`)
用 **RRF** 融合,产出该表的可见列 + `_score`,物化成 `_in` 交给下一段 SQL
(`search cards '…' | SELECT … FROM _in WHERE … ORDER BY _score DESC`)。
表只有一个可搜列时 `--col` 可省;多列必须显式指定。向量 / 全文就地长在业务表上
(DuckDB `vss`+`fts` 后端),insert 时同步 embed + jieba 落库,`insert` 返回即可搜。

1. **按相关度排序**:`_in` 带 `_score`(RRF 融合分),越相关越靠前。
2. **和结构化过滤组合**:接缝后一整条 SQL(`WHERE kind='…'` 等)干完。
3. **每列独立**:同一 query 文本 `--col title` / `--col body` 可得不同结果;
   多可搜列不带 `--col` → `QueryError`(旧的多 `search()` 单 SQL 形态随 UDF 退休)。
4. **中文 hybrid**:jieba 分词让 BM25 命中中文关键词,和向量 RRF 融合。
5. **删除后搜不到**:打墓碑的行不再被 search 段带出。
6. **rebuild 后仍可搜**:`rebuild()` 清空并从文件重灌(重新 embed + 重建索引)。
7. **和时间窗组合**:`ds_end` 早于写入日 → 搜不到;回溯到软删行还活着的那天 →
   照样搜得到(search 候选和结构化读共用同一条 as-of 谓词)。
8. **对无 searchable 的表 / 非 searchable 列 search** → `QueryError`(早失败)。

## 不在这测什么

- 管道机制本身(切分 / SQL 缺省 / 算子降级 / 参数分配)走 [`pipeline/`](../pipeline/)
- 结构化读写走 [`read_write/`](../read_write/) / 文件镜像走 [`file_mirror/`](../file_mirror/)
- 真 embedder(网络)—— 用确定性 `FakeEmbedder`(bag-of-chars,有排序信号、零依赖);中文用例靠 jieba+BM25 命中,不依赖 embedder 语义质量

## fixture 来源

- `db`(`tests/conftest.py`)—— `cards`(`issue` 可搜)+ `FakeEmbedder`
- `open_db`(自定义无 searchable 的 schema)—— 测「search 需要 searchable 表」
