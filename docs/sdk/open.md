# 拿句柄:`Seekbase.open` / `Seekbase.connect`

两种形态,一套 API:拿到 `db` 之后的所有调用(query / insert / task / …)**完全相同**,变的只有这一步。

## `Seekbase.open` — 嵌入(进程内 DuckDB)

```python
db = await Seekbase.open(
    data_dir,                    # str | Path:实例目录,自动创建
    *,
    schema=SCHEMA,               # 必填:声明式表结构(api/setup.md §3)
    embedder=None,               # Embedder;schema 有 searchable 列时必填
    search_backend="vss",        # "vss" | "lance":检索引擎后端
    policy=None,                 # Policy;缺省 read-only(policy.md)
    operators=None,              # list[Operator | type]:自定义算子(operator.md)
)
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `data_dir` | 是 | 实例目录:`duck.db` / `files/`(canonical 镜像)/ `tasks/`(task 日志 + 结果文件)/ `lance/`(lance 后端时)都在这;拷走目录 = 拷走整个库 |
| `schema` | 是 | 声明式表结构;解析失败 → `SchemaError` |
| `embedder` | 视情况 | schema 声明了 `searchable` 列却没给 → `EmbedderInvalid`;协议见 [embedder.md](embedder.md) |
| `search_backend` | 否 | `"vss"`(默认:DuckDB vss+fts 就地长在业务表,单文件、fd 恒定)/ `"lance"`(LanceDB 侧数据集,经 DuckDB `lance` 扩展;版本化、每写生成碎片——认领 fd 账,取舍见 [works/search.md §5](../works/search.md)) |
| `policy` | 否 | 算子授权策略,缺省 `Policy()`(read-only:`sh`/`jq` 被拒);见 [policy.md](policy.md) |
| `operators` | 否 | 追加注册的自定义算子(类或实例);重名 / 撞 SQL 关键字 → `QueryError` |

打开时做的事:建表(缺则建)、装检索扩展(按后端)、起写 worker、task 日志/结果文件按保留期 GC。

## `Seekbase.connect` — HTTP 客户端

```python
db = await Seekbase.connect(
    url,                 # "http://localhost:8000"
    *,
    api_key=None,        # bearer token(server 配了才需要)
    transport=None,      # 可选 httpx transport(测试用 ASGITransport)
)
```

- **不做握手**:第一次真正调用才打到 server;schema / embedder / policy 都在 server 端。
- 远程形态**不支持** `db.stream`(嵌入专属,见 [stream.md](stream.md));其余方法同形。
- 慢查询在 HTTP 上自动带 `wait_ms=5000` 语义:超时升级成 task(见 [query.md](query.md#as_task)),客户端 `query()` 内部对 202 响应返回 task id。

## 生命周期

```python
db.ready                 # bool:句柄可用(远程形态恒 True,不探活)
await db.close()         # 停流 → 取消后台 task → 停写 worker → 关库;幂等
async with await Seekbase.open(...) as db:      # 上下文管理器,退出即 close
    ...
db.services              # 嵌入形态:进程内服务层(read/write/admin/stream/task);远程为 None
```

`close()` 会对全部读 cursor 发 `interrupt()`——跑飞的查询不会挂住关库([works/task.md §5](../works/task.md))。

## 错误

| 情况 | 异常 |
|---|---|
| schema 解析失败 | `SchemaError` |
| 有 searchable 列但没给 embedder | `EmbedderInvalid` |
| `search_backend` 不是 `vss`/`lance` | `QueryError` |
| 自定义算子重名 / 撞 SQL 引导关键字 / 无任何可执行格 | `QueryError` |
