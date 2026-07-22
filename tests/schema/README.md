# schema — 声明式 SCHEMA 校验 + 早失败

## 这个场景在测什么

schema 是**声明的(有序 list 形态)、在 `open` 时校验一次**,坏形状当场报错、不拖到运行时:

1. **`parse_schema` 校验**:`SCHEMA` 是 list、每项有 `table`(唯一);`columns` 是
   `{name, type}` list、列名唯一;`primary` 指向一个已声明的 `str`/`int` 列;不许
   声明保留元数据列(`ds`/`created_at`/`deleted_ds`/`deleted_at`);列类型合法
   (含 `decimal(p,s)` 的 `p`/`s`);`searchable` 列须是 `str`。
2. **高级类型 DDL round-trip**:`decimal` / `timestamptz` / `json` 列能建表 + 写入。
3. **未知列被拒**:`insert` 碰到 schema 里没有的列 → `QueryError`。
4. **searchable 列必须给 embedder**:声明了可搜列却不注入 embedder → 在 `open`
   就 `EmbedderInvalid`,而不是等到第一次 `search`。

## 不在这测什么

- SQL 读写走 [`read_write/`](../read_write/);语义 `search` 段走 [`search/`](../search/)
- schema 演进 / `_meta` 指纹 —— 后续,尚未实现

## fixture 来源

- `db`(`tests/conftest.py`)—— 未知列用标准库
- `open_db` / `parse_schema`(直接构造坏 / 高级类型 schema)—— 校验与 DDL 用例
