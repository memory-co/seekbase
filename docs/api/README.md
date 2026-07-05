# API 参考

seekbase 的本地 API,所有接口收发 JSON。**读同步、写异步**:

- **读**:`POST /v1/query` 传一段 SQL(语义检索 `search()` 与时光机 `as_of` 都在这一个接口里),同步返回行。
- **写**:提交类接口(insert / delete / rebuild / vacuum)**不阻塞**——返回一个 `ticket`,再用 `GET /v1/writes/{ticket}` 轮询这次写入的状态。

拿句柄、起 server、声明 schema、注入 embedder 都在 [setup.md](setup.md)。

```
Query     POST  /v1/query              读:SQL(含 search + as_of),返回行            → query.md
Insert    POST  /v1/insert             写(异步):提交要写的行,返 ticket             → insert.md
Delete    POST  /v1/delete             写(异步):按条件打墓碑,返 ticket             → delete.md
Writes    GET   /v1/writes/{ticket}    查一次写入的状态(pending/done/failed)         → insert.md
Rebuild   POST  /v1/rebuild            从文件重建派生层(异步),返 ticket             → admin.md
Vacuum    POST  /v1/vacuum             物理清墓碑 / 丢历史(异步),返 ticket           → admin.md
Health    GET   /v1/health             健康:{"ready": bool}                          → admin.md
```

## 鉴权

server 配了 `api_key` 时每个请求须带 `Authorization: Bearer <api_key>`;不匹配 → `401`,`{"error": {"type": "Unauthorized", "message": "bad api key"}}`。单个可选 bearer token,多租户 auth 非目标。

## 错误

统一响应体 `{"error": {"type": <类名>, "message": <文本>}}`,状态码按类型:

| 状态 | 异常类型 | 含义 |
|---|---|---|
| 400 | `QueryError` | 未知表 / 列、SQL 语法错、`as_of` 格式错 |
| 400 | `ReadOnlyError` | `query` 传了非 `SELECT`/`WITH` 语句 |
| 400 | `SchemaError` / `EmbedderInvalid` | schema 校验 / embedder 契约失败 |
| 401 | `Unauthorized` | bearer token 不匹配 |
| 404 | `NotFound` | `ticket` 不存在 |
| 501 | `NotSupportedYet` | 已设计、当前里程碑未实现 |
| 503 | `SeekbaseUnavailable` | 底层开不了 / 不可服务 |
| 500 | `Internal` | 未预期内部异常 |

**错误保型过线**:客户端(`connect`)收到非 200 时按 `error.type` 重建同类型异常并抛出——server 侧的 `ReadOnlyError` 在客户端还是 `ReadOnlyError`。层级:`SeekbaseError`(基类)→ `SeekbaseUnavailable` / `SchemaError` / `EmbedderInvalid` / `ReadOnlyError` / `QueryError` / `NotSupportedYet`。

## 设计要点

- **读同步、写异步**。`query` 同步返回;写(insert/delete/rebuild/vacuum)返回 `ticket`,真正兑现是异步的。**提交后要等 ticket 到 `done`,这次写入才保证被 `query`/`search` 读到**(读己之写)。
- **只增、引擎强制**:没有 update/upsert;`delete` 唯一语义是打 `deleted_at` 墓碑(非物理删),`query` 默认自动滤掉墓碑行。物理删只有 `vacuum`。
- **一个读接口,SQL 为面**:结构化查询、语义检索(`search()` 函数)、时光机(`as_of`)全在 `POST /v1/query` 里,不为搜索单开接口。
- **时光机 per-request**:`as_of` 是 `query` 的参数;一个 server 能同时服务各自 `as_of` 的多个请求。
- **两形态同一套语义**:函数形态(`Seekbase.open` 进程内 / `Seekbase.connect` 客户端)构造的就是这些请求,调用代码逐字节相同。

## 文档清单

| 文档 | 覆盖 |
|---|---|
| [query.md](query.md) | 读:SQL + `search()` + `as_of` |
| [insert.md](insert.md) | 异步写:提交 + 状态查询 |
| [delete.md](delete.md) | 异步删:打墓碑 |
| [admin.md](admin.md) | `rebuild` / `vacuum` / `health` |
| [setup.md](setup.md) | `open` / `connect` / `serve` + schema 声明 + embedder 注入 |
