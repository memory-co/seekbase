# schema — 声明式 SCHEMA 校验 + 早失败

## 这个场景在测什么

schema 是**声明的、在 `open` 时校验一次**,坏的形状当场报错、不拖到运行时:

1. **`parse_schema` 校验**:每表恰一个 `primary`、不许声明保留元数据列
   (`created_at`/`deleted_at`)、列类型只能是 `str/int/float/bool`、`files`
   模板里的 `{占位符}` 必须是真实列。
2. **未知列被拒**:`eq()` / `insert()` 碰到 schema 里没有的列 → `QueryError`,
   列名走白名单(顺带挡注入)。
3. **searchable 列必须给 embedder**:声明了可搜列却不注入 embedder → 在 `open`
   就 `EmbedderInvalid`,而不是等到第一次 `search()`。
4. **`search()` 已接受、但 M3 才落**:算子在链上合法,执行时抛 `NotSupportedYet`
   —— 让链的形状从第一天就稳定。

## 不在这测什么

- 结构化查询语义走 [`basic_orm/`](../basic_orm/)
- 向量 `search()` 真正跑通 —— M3
- schema 演进 / `_meta` 指纹 —— M5,尚未实现

## fixture 来源

- `db`(`tests/conftest.py`)—— 未知列 / `search()` 延后用标准库
- `open_db(embedder=None)` / `parse_schema`(直接构造坏 schema)—— 校验失败用例
