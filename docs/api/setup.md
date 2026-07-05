# Setup:拿句柄 / 起 server / schema / embedder

数据接口见 [query](query.md) / [insert](insert.md) / [delete](delete.md) / [admin](admin.md)。本页讲怎么把库跑起来:拿 `db` 句柄、起 HTTP server、声明 schema、注入 embedder。

## 1. 拿句柄

### `Seekbase.open` — 嵌入(进程内 DuckDB)

```python
db = await Seekbase.open(
    data_dir,           # str | Path:实例目录,自动创建
    *,
    schema=SCHEMA,      # 声明式表结构(见 §3)
    embedder=None,      # Embedder;schema 有 searchable 列时必填(见 §4)
)
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `data_dir` | 是 | 实例目录;`duck.db` / `lance/` / `files/` 都落这里,拷走目录 = 拷走整个库 |
| `schema` | 是 | 声明式表结构(§3) |
| `embedder` | 视情况 | schema 有 `searchable` 列时必填,否则 `EmbedderInvalid` |

> 时光机 / 时间窗**不在连接上**——是 `query` 的 `ds_start`/`ds_end` 参数(见 [query.md](query.md#时间窗-ds_start--ds_end日期分区));句柄本身不绑时间。

### `Seekbase.connect` — HTTP 客户端

```python
db = await Seekbase.connect(
    url,                # "http://localhost:8000"
    *,
    api_key=None,       # bearer token(server 配了才需要)
    transport=None,     # 可选 httpx transport(测试用 ASGITransport)
)
```

- **不做握手**:第一次真正查询才打到 server。
- schema 与 embedder 都在 **server 端**,客户端不带;之后 `db.query(...)` / `db.insert(...)` 用法与嵌入**完全相同**。

### 通用:`ready` / `close`

```python
db.ready            # bool(对应 GET /v1/health 的 ready)
await db.close()    # 嵌入:关 DuckDB/Lance/文件;客户端:关 httpx
async with await Seekbase.open(data_dir, schema=SCHEMA) as db:
    ...             # 退出自动 close()
```

## 2. 起 server

server 持有 schema 与 embedder、拥有数据目录;客户端 `connect` 连它。

```python
from seekbase.server import seekbase_server, serve

db = await Seekbase.open("./data", schema=SCHEMA, embedder=embedder)

# 方式一:拿裸 ASGI app,用你自己的 runner 跑
app = seekbase_server(db, api_key="secret")
import uvicorn; uvicorn.run(app, host="0.0.0.0", port=8000)

# 方式二:便捷函数;runner 外部注入,缺省用 uvicorn(装了才行)
serve(db, host="0.0.0.0", port=8000, api_key="secret", runner=None)
```

| 函数 | 说明 |
|---|---|
| `seekbase_server(db, *, api_key=None)` | 返回裸 ASGI app(**零 web 框架依赖**),挂进任意 ASGI server |
| `serve(db, *, host, port, api_key=None, runner=None)` | 便捷启动;`runner` 是任意 `runner(app, host=, port=)` 可调用,**始终外部提供**,不是 seekbase 依赖 |

暴露的端点见 [README.md](README.md)。

## 3. 声明 schema

表结构声明一次,DDL / 双引擎同步 / 文件镜像全由 seekbase 管。`open` / server 启动时校验一次——**坏形状当场报错**。设计与推导(一处声明 → 三引擎)见 [`../works/schema.md`](../works/schema.md)。

```python
SCHEMA = {
    "cards": {
        "columns": {"card_id": "str primary", "issue": "str", "kind": "str"},
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

**`columns`**

- 类型:`str` / `int` / `float` / `bool`;修饰 `primary`——**每表恰一个主键**。
- **声明式、不从首行推断**(避免首行 null 把列判成 string)。
- `ds` / `created_at` / `deleted_ds` / `deleted_at` 是**引擎代管的元数据列**,自动加;**不许自己声明**。两对(创建 / 删除)日期字段:`ds`/`deleted_ds`(天,`YYYYMMDD`,分区 / 时光机判定)+ `created_at`/`deleted_at`(精确时刻)。完整设计见 [`../works/time_machine.md`](../works/time_machine.md)。

**`searchable`**

- 列出哪些列可被 `search()` 语义检索。声明了 → `insert` 时该列文本自动 embed、`search()` 自动查。
- 有 `searchable` 列 ⇒ **必须注入 embedder**,否则 `EmbedderInvalid`。没有则是纯 DuckDB 表,零向量开销。

**`files`**(本地镜像,详见 [`../works/store.md`](../works/store.md))

- 字符串 = 一行一文件(json):`"cards/{card_id}.json"`(路径模板含**主键**)。
- 字典 = 显式模式:`{"path": "...", "mode": "json" | "jsonl"}`(jsonl 按**分组键**追加)。
- 模板 `{占位符}` 必须是已声明列;没声明 `files` = 无镜像。

**校验规则**(`seekbase.schema.parse_schema`)

| 规则 | 违反 → |
|---|---|
| 每表恰一个 `primary` | `SchemaError` |
| 不许声明 `created_at`/`deleted_at` | `SchemaError` |
| 列类型 ∈ `str/int/float/bool` | `SchemaError` |
| `searchable` / `files` 占位符须是已声明列 | `SchemaError` |
| 有 `searchable` 却无 embedder | `EmbedderInvalid` |

## 4. 注入 embedder

seekbase 核心只认一个**注入协议**,不绑定模型。调用方永远不见向量——只写文本。

```python
class Embedder(Protocol):
    @property
    def dim(self) -> int: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...   # 可同步可异步
```

默认实现 `ApiEmbedder`(核心自带,基于 `httpx`,不加载本地模型):

```python
from seekbase.embedders import ApiEmbedder

embedder = ApiEmbedder(
    base_url="https://api.openai.com/v1",   # OpenAI 兼容 /embeddings
    api_key="sk-…", model="text-embedding-3-small", dim=1536,
    batch_size=128, max_retries=3, timeout=30.0,
)
db = await Seekbase.open(data_dir, schema=SCHEMA, embedder=embedder)
```

- 调 `POST {base_url}/embeddings`,取 `data[].embedding`;维度不符 / 失败到顶 → `EmbedderInvalid`。
- embedder 在 **server / 进程端**注入;`connect` 的客户端不带,embedding 在 server 上算。
- **TODO**:本地 sentence-transformers 版(`SentenceTransformerEmbedder`),同一协议(DESIGN §10)。
