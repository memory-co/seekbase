# 拿句柄 / 起 server

数据操作见 [operations.md](operations.md);本页只讲怎么拿到 `db`,以及怎么把它暴露成 HTTP 服务。

## `Seekbase.open` — 嵌入(进程内 DuckDB)

```python
db = await Seekbase.open(
    data_dir,           # str | Path:实例目录,自动创建
    *,
    schema=SCHEMA,      # 声明式表结构(见 schema.md)
    embedder=None,      # Embedder;schema 有 searchable 列时必填(见 embedders.md)
    as_of=None,         # None=当前态(可写);ISO 时间点=只读时光机
)
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `data_dir` | 是 | 实例目录;`duck.db` / `lance/` / `files/` 都落这里,拷走目录 = 拷走整个库 |
| `schema` | 是 | 声明式表结构 |
| `embedder` | 视情况 | schema 有 `searchable` 列时必填,否则 `EmbedderInvalid` |
| `as_of` | 否 | 给 ISO 字符串则连接**只读**,查询回退到那个时刻 |

## `Seekbase.connect` — HTTP 客户端

```python
db = await Seekbase.connect(
    url,                # "http://localhost:8000"
    *,
    api_key=None,       # bearer token(server 配了才需要)
    as_of=None,         # 只读回退,随每请求带给 server
    transport=None,     # 可选 httpx transport(测试用 ASGITransport)
)
```

- **不做握手**:第一次真正查询才打到 server。
- schema 与 embedder 都在 **server 端**,客户端不带。
- 之后 `db.table(...)` / `db.sql(...)` 用法与嵌入**完全相同**。

## 通用:`ready` / `close`

```python
db.ready            # bool(对应 GET /v1/health 的 ready)
await db.close()    # 嵌入:关 DuckDB/Lance/文件;客户端:关 httpx
async with await Seekbase.open(data_dir, schema=SCHEMA) as db:
    ...             # 退出自动 close()
```

## 起 server

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

暴露的端点见 [README.md](README.md) 与 [operations.md](operations.md)。
