# Server(HTTP 形态)

把一个嵌入 `Seekbase` 暴露成 HTTP 服务。server 持有 schema 与 embedder、拥有数据目录;客户端用 `Seekbase.connect` 连它,调用代码与嵌入形态一模一样。

## `seekbase_server(db, *, api_key=None)` — 建 ASGI app

```python
from seekbase import Seekbase
from seekbase.server import seekbase_server

db = await Seekbase.open("./data", schema=SCHEMA, embedder=embedder)
app = seekbase_server(db, api_key="secret")   # 裸 ASGI app,零 web 框架依赖
```

- 返回一个**手写的 ASGI app**(无 web 框架依赖)。
- 跑它的 **ASGI runner 由你外部注入**:`uvicorn.run(app, ...)` / hypercorn / gunicorn,或挂进更大的应用。

## `serve(db, *, host, port, api_key=None, runner=None)` — 便捷启动

```python
from seekbase.server import serve

serve(db, host="0.0.0.0", port=8000, api_key="secret")   # 阻塞
serve(db, runner=uvicorn.run)                             # 显式注入 runner
```

- `runner` 是任意 `runner(app, host=…, port=…)` 可调用;缺省时用 uvicorn(装了才行),否则报清晰错误指向 `seekbase_server`。
- **runner 始终外部提供**,不是 seekbase 依赖。

## HTTP 端点

### `POST /v1/execute`

跑一个序列化的操作(就是 `QueryBuilder` 构造的那个 `Request`)。

**请求 body**(字段全集):

```json
{
  "op": "select | count | insert | delete | search | sql | flush | rebuild | vacuum",
  "table": "cards",
  "columns": ["card_id", "issue"],
  "predicates": [{"op": "eq", "column": "kind", "value": "issue"}],
  "orders": [["created_at", true]],
  "limit": 20,
  "offset": null,
  "rows": [{"card_id": "c1", "issue": "…"}],
  "statement": "SELECT …",
  "before": "2026-06-01T00:00:00Z",
  "as_of": null
}
```

- 只需带与本次 `op` 相关的字段;其余给 `null` / 空。
- `as_of` 非 null → 只读回退;写类 `op` 会被 `ReadOnlyError` 挡。

**响应**:

```json
200 {"result": <list | int | null>}
4xx/5xx {"error": {"type": "ReadOnlyError", "message": "…"}}
```

- `result` 类型随 `op`:`select`/`sql` → 行数组;`count`/`delete` → int;`insert`/`flush` → null。
- 错误保型:`error.type` 是异常类名,客户端据此重建同类型异常(映射见 [errors.md](errors.md))。

### `GET /v1/health`

```json
200 {"ready": true}
```

### 鉴权

server 配了 `api_key` 时,每个请求须带:

```
Authorization: Bearer <api_key>
```

不匹配 → `401 {"error": {"type": "Unauthorized", "message": "bad api key"}}`。单个可选 bearer token;多租户 auth 非目标。

## 客户端侧

见 [connection.md](connection.md) 的 `Seekbase.connect`。客户端把链序列化成上面的请求、解析 `result`、把 `error` 还原成异常——这些都在 `HttpExecutor` 内部,调用方无感。
