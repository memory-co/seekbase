# API 参考

seekbase 的本地 API。所有**数据操作**打到**一个端点** `POST /v1/execute`（RPC 风格，body 带 `op`），外加一个 `GET /v1/health`。函数形态（`Seekbase.open` 进程内 / `Seekbase.connect` 客户端）构造的就是这些请求——每个 `op` 的文档给出对应的 Python 一行。

- 拿句柄 / 起 server 见 [setup.md](setup.md)
- 每个操作的请求 / 响应 / 错误见 [operations.md](operations.md)
- schema / embedder（server 端配置）见 [schema.md](schema.md) / [embedders.md](embedders.md)
- 存储与文件镜像设计见 [`../works/store.md`](../works/store.md)

```
Execute   POST  /v1/execute     跑一个操作（op 见下），body 是统一信封
Health    GET   /v1/health      健康：{"ready": bool}
```

## 操作一览（`op`）

| `op` | 需要字段 | `result` | 函数形态 | 状态 |
|---|---|---|---|---|
| `select` | `table` `columns` `predicates` `orders` `limit` `offset` `as_of` | `list`（行） | `db.table(t).select(...)` | ✅ |
| `count` | `table` `predicates` `as_of` | `int` | `db.table(t)...count()` | ✅ |
| `insert` | `table` `rows` | `null` | `db.table(t).insert(rows)` | ✅ |
| `delete` | `table` `predicates` | `int`（墓碑数） | `db.table(t).delete()...` | ✅ |
| `search` | `table` `predicates` … | `list`（带 `_score`） | `db.table(t).search(text)...` | `[M3]` 501 |
| `sql` | `statement` `as_of` | `list`（行） | `db.sql(stmt)` | ✅ 只读 |
| `flush` | — | `null` | `db.flush()` | `[M3]` no-op |
| `rebuild` | — | `null` | `db.rebuild()` | `[M2]` 501 |
| `vacuum` | `before` | `null` | `db.vacuum(before=…)` | `[M4]` 501 |

## 请求 / 响应信封

```jsonc
POST /v1/execute
{
  "op": "select",                 // 见上表
  "table": "cards",
  "columns": ["card_id", "issue"],
  "predicates": [{"op": "eq", "column": "kind", "value": "issue"}],
  "orders": [["created_at", true]],   // [列, desc?]
  "limit": 20, "offset": null,
  "rows": [], "statement": null, "before": null,
  "as_of": null                   // 非 null = 只读回退到该时刻
}
```

```jsonc
200      {"result": <list | int | null>}
4xx/5xx  {"error": {"type": "<异常类名>", "message": "<文本>"}}
```

只填与本次 `op` 相关的字段，其余给 `null` / 空数组。`result` 类型随 `op`（见上表）。

**谓词编码**（`predicates` 每项）：`{"op": "<eq|neq|gt|gte|lt|lte|like|ilike|in_|is_>", "column": "<列>", "value": <值>}`。`in_` 的 `value` 是数组；`is_` 的 `value` 为 `null` = `IS NULL`。
**排序编码**（`orders`）：`[[列, desc:bool], …]`。

## 鉴权

server 配了 `api_key` 时每个请求须带 `Authorization: Bearer <api_key>`；不匹配 → `401`，`{"error": {"type": "Unauthorized", "message": "bad api key"}}`。单个可选 bearer token，多租户 auth 非目标。

## 错误

统一响应体 `{"error": {"type": <类名>, "message": <文本>}}`，状态码按类型：

| 状态 | 异常类型 | 含义 |
|---|---|---|
| 400 | `QueryError` | 未知表 / 列、不支持的算子 |
| 400 | `ReadOnlyError` | 往 `as_of` 连接写；`sql()` 传了非只读语句 |
| 400 | `SchemaError` / `EmbedderInvalid` | schema 校验 / embedder 契约失败 |
| 401 | `Unauthorized` | bearer token 不匹配 |
| 501 | `NotSupportedYet` | 已设计、当前里程碑未实现（如 `search`） |
| 503 | `SeekbaseUnavailable` | 底层开不了 / 不可服务 |
| 500 | `Internal` | 未预期内部异常 |

- **错误保型过线**:客户端(`connect`)收到非 200 时,按 `error.type` 重建同类型异常并抛出——server 侧的 `ReadOnlyError` 在客户端还是 `ReadOnlyError`,同样的 `except` 生效。
- 层级:`SeekbaseError`(基类)→ `SeekbaseUnavailable` / `SchemaError` / `EmbedderInvalid` / `ReadOnlyError` / `QueryError` / `NotSupportedYet`。

## 设计要点

- **单端点 RPC**:所有数据操作走 `POST /v1/execute`,`op` 分派。`QueryBuilder` 链在客户端组装成一个信封发出;server 端解出来跑。**两形态调用代码逐字节相同**,差别只在 `open` 还是 `connect`。
- **只增、引擎强制**:没有 `update`/`upsert`;`delete` 唯一语义是打 `deleted_at` 墓碑,正常查询自动滤掉(非物理删)。
- **`as_of` per-request**:非 null 则该请求只读回退到那个时刻;写类 `op` 带 `as_of` → `ReadOnlyError`。一个 server 能同时服务各自 `as_of` 的多个客户端。
- **`sql` 只读**:语句须以 `SELECT`/`WITH` 开头(逃生舱:join/聚合/窗口/对账);写只能走 ORM。

## 文档清单

| 文档 | 覆盖 |
|---|---|
| [setup.md](setup.md) | `Seekbase.open` / `connect` / `close`;起 server:`seekbase_server` / `serve` |
| [operations.md](operations.md) | 每个 `op` 的请求 / 响应 / 副作用 / 错误 + 函数形态 |
| [schema.md](schema.md) | 声明式 SCHEMA(`columns` / `searchable` / `files`) |
| [embedders.md](embedders.md) | `Embedder` 协议 + 默认 `ApiEmbedder` |
