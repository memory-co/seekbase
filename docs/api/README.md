# seekbase API 参考

seekbase 有两种使用形态,**共用一套语义**。文档按「先 HTTP、后函数」组织:

1. **[http.md](http.md) — HTTP 形态(权威契约)**。所有查询最终都序列化成 `POST /v1/execute` 的一个操作;这份协议是底层契约,先读它。
2. **[functions.md](functions.md) — 函数形态(Python)**。`Seekbase.open`(嵌入)/ `Seekbase.connect`(客户端)构造的就是上面那些 HTTP 请求;调用代码两形态逐字节相同。

配置与共享定义:

| md | 覆盖 |
|---|---|
| [http.md](http.md) | 鉴权、端点、`Request` 线格式、每个 `op` 的请求/响应、`as_of`、错误→状态码 |
| [functions.md](functions.md) | `open`/`connect`、查询链与算子、`sql`/`flush`/`rebuild`/`vacuum`、server 启动 |
| [schema.md](schema.md) | 声明式 SCHEMA(`columns`/`searchable`/`files`)—— server 端配置 |
| [embedders.md](embedders.md) | `Embedder` 协议 + 默认 `ApiEmbedder` —— server 端配置 |
| [errors.md](errors.md) | 错误层级 + 错误↔HTTP 状态码映射 |

> **M1 现状**:各篇按目标 API 写,未落的用 `[M2]`/`[M3]`/`[M4]` 标出——`search` 抛 `NotSupportedYet`(M3),`flush` no-op(M3),`rebuild`/`vacuum` 抛 `NotSupportedYet`(M2/M4)。
