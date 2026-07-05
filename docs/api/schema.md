# 声明式 SCHEMA

表结构声明一次,DDL / 双引擎同步 / 文件镜像全由 seekbase 管。schema 在 `open`(嵌入)或 server 启动(HTTP)时校验一次——**坏形状当场报错**,不拖到运行时。

```python
SCHEMA = {
    "cards": {
        "columns": {"card_id": "str primary", "issue": "str",
                    "kind": "str", "created_at": "str"},
        "searchable": ["issue"],                 # 可 search() 的列(写入自动 embed)
        "files": "cards/{card_id}.json",         # 本地 JSON 镜像(可 grep)
    },
    "rounds": {
        "columns": {"session_id": "str", "idx": "int", "text": "str"},
        "searchable": ["text"],
        "files": {"path": "sessions/{session_id}/rounds.jsonl", "mode": "jsonl"},
    },
}
```

## `columns`

- 类型:`str` / `int` / `float` / `bool`。
- 修饰:`primary`——**每表恰一个主键**(做 id 对齐)。
- **声明式、不从首行推断**(避免首行 null 把列判成 string)。
- `created_at` / `deleted_at` 是**引擎代管的元数据列**,自动加;**不许自己声明**(否则 `SchemaError`)。

## `searchable`

- 列出哪些列可被 `search()` 语义检索。
- 声明了 → `insert` 时该列文本自动 embed 进向量侧、`search()` 自动查。
- 有 `searchable` 列 ⇒ **必须注入 embedder**,否则 `open` 时 `EmbedderInvalid`。
- 没有 `searchable` 列的表 = 纯 DuckDB 表,**零向量开销**。

- 字符串 = 一行一文件(json):`"cards/{card_id}.json"`(路径模板,列值填充)。
- 字典 = 显式模式:`{"path": "...", "mode": "json" | "jsonl"}`。
- 模板里的 `{占位符}` 必须是**已声明的列**。
- 没声明 `files` 的表 = 无镜像(纯派生/日志表不落盘)。

**json 还是 jsonl,由 schema 声明,不由 seekbase 猜**——seekbase 业务无关,不知道某张表「是流水所以该追加」。判据是**结构性**的、也是正确性条件:

| 模式 | 路径模板 vs 行 | 模板里的键 | 为什么 |
|---|---|---|---|
| json(一行一文件) | 1:1 | **主键** | per-row 文件须唯一定位,否则两行撞一个文件 |
| jsonl(追加) | 1:多 | **非主键分组列** | 键不唯一 → 同路径来多行 → 只能 append |

> 一句话:**模板里放主键就是 json,放分组键就是 jsonl**。这从 schema 结构就能推出,与业务无关。详见 [works/store.md](../works/store.md)。

## 校验规则(`seekbase.schema.parse_schema`)

`open` / `serve` 内部调用,也可直接用于校验:

| 规则 | 违反 → |
|---|---|
| 每表恰一个 `primary` | `SchemaError` |
| 不许声明 `created_at`/`deleted_at` | `SchemaError` |
| 列类型 ∈ `str/int/float/bool` | `SchemaError` |
| `searchable` 列须是已声明列 | `SchemaError` |
| `files` 占位符须是已声明列 | `SchemaError` |
| 有 `searchable` 却无 embedder | `EmbedderInvalid`(在 `open`) |

## 两种形态

- **函数形态**:`Seekbase.open(dir, schema=SCHEMA, embedder=…)` 直接传。
- **HTTP 形态**:schema 是 **server 端配置**——在起 server 的那段代码里传给 `Seekbase.open`(见 [server.md](server.md));客户端 `connect` **不带 schema**,由 server 校验列名等。
