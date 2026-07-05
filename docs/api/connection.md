# 连接与生命周期

拿到一个 `Seekbase` 句柄的两条路——`open`(嵌入)与 `connect`(HTTP 客户端)——之后所有接口都一样。

## `Seekbase.open` — 函数形态(嵌入)

```python
db = await Seekbase.open(
    data_dir,              # str | Path:实例目录,自动创建
    *,
    schema=SCHEMA,         # 声明式表结构(见 schema.md)
    embedder=None,         # Embedder;schema 有 searchable 列时必填(见 embedders.md)
    as_of=None,            # None=当前态(可写);给时间点=只读时光机
)
```

- 进程内打开 DuckDB(以及后续的 LanceDB / 文件镜像),`data_dir` 就是这个实例的全部——拷走目录 = 拷走整个库。
- `as_of` 给了字符串(ISO-8601)后,这个连接**只读**:任何写入抛 `ReadOnlyError`,查询只看得见那个时刻及之前的世界。

## `Seekbase.connect` — HTTP 形态(客户端)

```python
db = await Seekbase.connect(
    url,                   # str:server 基地址,如 "http://localhost:8000"
    *,
    api_key=None,          # bearer token(server 配了才需要)
    as_of=None,            # 同 open:只读回退,per-request 传给 server
    transport=None,        # 可选 httpx transport(测试用 ASGITransport)
)
```

- **不做握手**:`connect` 只构造一个 HTTP 客户端;第一次真正的查询才打到 server。
- schema 与 embedder 都在 **server 端**,客户端不带。
- `as_of` 随每个请求带给 server,由 server 应用回退与只读闸——所以一个 server 能同时服务各自 `as_of` 的多个客户端。

> HTTP 形态没有单独的 "connect" 端点;要探活用 `GET /v1/health`(见 server.md)。

## `ready` / `close` / 上下文管理器(两形态相同)

```python
db.ready            # bool:底层可用性(False → 宿主应回 503 / 降级)
await db.close()    # 排干 outbox、停 consumer、关连接 / 关 HTTP 客户端

async with await Seekbase.open(data_dir, schema=SCHEMA) as db:
    ...             # 退出时自动 close()
```

| | 函数形态 | HTTP 形态 |
|---|---|---|
| 打开 | `Seekbase.open(dir, schema=…, embedder=…)` | `Seekbase.connect(url, api_key=…)` |
| `ready` | 本地引擎可用性 | 客户端就绪(可配合 `GET /v1/health`) |
| `close` | 关 DuckDB/Lance/文件 | 关 httpx 客户端 |
| 只读回退 | `as_of=` 引擎级下沉为 per-call | `as_of=` 每请求带给 server |
