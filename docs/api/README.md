# seekbase API 参考

按「一类接口一个 md」组织。每个接口都同时给**两种形态**:

- **函数形态(embedded)**:`await Seekbase.open(...)` 后进程内直接调。
- **HTTP 形态(server)**:`await Seekbase.connect(url)` 后同样的调用代码走 HTTP;想不经 Python 客户端、直接打 HTTP 的,每篇给出对应的 `POST /v1/execute` 线格式。

> 两形态**调用代码逐字节相同**——差别只在你用 `open` 还是 `connect` 拿 `db`。HTTP 形态下,查询链会被序列化成一个 `POST /v1/execute` 请求(线格式见 [server.md](server.md))。

## 分类

| md | 覆盖 |
|---|---|
| [connection.md](connection.md) | `open` / `connect` / `ready` / `close` / async context manager |
| [query.md](query.md) | ORM 链:`table` / `select` / `insert` / `delete` / `count` / `search` + 过滤/排序/分页算子 |
| [sql-and-admin.md](sql-and-admin.md) | `sql`(只读直查)/ `flush` / `rebuild` / `vacuum` |
| [schema.md](schema.md) | 声明式 SCHEMA(`columns` / `searchable` / `files`)|
| [embedders.md](embedders.md) | `Embedder` 协议 + 默认 `ApiEmbedder` |
| [server.md](server.md) | `seekbase_server` / `serve` + HTTP 端点与线格式 |
| [errors.md](errors.md) | 错误层级 + 错误↔HTTP 状态码映射 |

## M1 现状标注

各篇按**目标 API** 写;当前里程碑(M1)未落的用 `[M3]` / `[M4]` 等标出:`search()` 已接受但执行抛 `NotSupportedYet`(M3),`flush()` 为 no-op(M3),`rebuild()`/`vacuum()` 抛 `NotSupportedYet`(M2/M4)。
