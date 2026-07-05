# HTTP 形态(权威契约)

seekbase 的两种形态里,**HTTP 是底层契约**:函数形态的每一次查询,最终都序列化成这里的一个 `POST /v1/execute` 请求。先把这份协议说清楚,函数形态(见 [functions.md](functions.md))就是「按同样的语义在本地/远端构造这些请求」。

起一个 server 见 [functions.md#server-启动](functions.md#server-启动);本篇只讲**线上协议**。

## 鉴权

server 配了 `api_key` 时,每个请求须带:

```
Authorization: Bearer <api_key>
```

不匹配 → `401`,`{"error": {"type": "Unauthorized", "message": "bad api key"}}`。单个可选 bearer token,多租户 auth 非目标。

## 端点

### `GET /v1/health`

```
GET /v1/health
→ 200 {"ready": true}
```

### `POST /v1/execute`

跑**一个操作**。请求体是一个统一的信封(下面的字段全集),只需填与本次 `op` 相关的字段,其余给 `null` / 空数组。

```jsonc
POST /v1/execute
Content-Type: application/json
{
  "op":         "select",          // 见下「操作表」
  "table":      "cards",           // 目标表(sql/flush/rebuild/vacuum 不需要)
  "columns":    ["card_id","issue"],
  "predicates": [{"op":"eq","column":"kind","value":"issue"}],
  "orders":     [["created_at", true]],   // [列, desc?]
  "limit":      20,
  "offset":     null,
  "rows":       [],                // insert 用
  "statement":  null,              // sql 用
  "before":     null,              // vacuum 用
  "as_of":      null               // 非 null = 只读回退到该时刻
}
```

**响应信封**:

```jsonc
200  {"result": <list | int | null>}
4xx/5xx  {"error": {"type": "<异常类名>", "message": "<文本>"}}
```

- `result` 类型随 `op`(见操作表)。
- 出错时 `error.type` 是异常类名,客户端据此重建同类型异常;状态码映射见 [errors.md](errors.md)。

## 操作表(`op`)

| `op` | 需要的字段 | `result` | 备注 |
|---|---|---|---|
| `select` | `table` `columns` `predicates` `orders` `limit` `offset` `as_of` | `list`(行) | `columns` 空 = 声明列 + `created_at` |
| `count` | `table` `predicates` `as_of` | `int` | |
| `insert` | `table` `rows` | `null` | 写;`as_of` 非 null → `ReadOnlyError` |
| `delete` | `table` `predicates` | `int` | 打墓碑行数;写;非物理删 |
| `search` | `table` `predicates` … | `list`(带 `_score`) | **`[M3]`** M1 返回 `501` |
| `sql` | `statement` `as_of` | `list`(行) | 只读;非 `SELECT/WITH` → `ReadOnlyError` |
| `flush` | — | `null` | **`[M3]`** M1 为 no-op |
| `rebuild` | — | `null` | **`[M2]`** M1 返回 `501` |
| `vacuum` | `before` | `null` | **`[M4]`** M1 返回 `501` |

### 谓词编码(`predicates`)

```jsonc
{"op": "<eq|neq|gt|gte|lt|lte|like|ilike|in_|is_>", "column": "<列>", "value": <值>}
```

- `in_` 的 `value` 是数组:`{"op":"in_","column":"card_id","value":["c1","c2"]}`(空数组匹配空集)。
- `is_` 的 `value` 为 `null` 表示 `IS NULL`。
- 未知列 → `400` `QueryError`(列名走白名单,顺带挡注入);值走参数绑定。

### 排序编码(`orders`)

`[[列, desc:bool], ...]`,如 `[["created_at", true], ["n", false]]`。

## 各操作示例

**select** — `SELECT card_id, issue FROM cards WHERE kind='issue' ORDER BY created_at DESC LIMIT 20`:

```jsonc
{"op":"select","table":"cards","columns":["card_id","issue"],
 "predicates":[{"op":"eq","column":"kind","value":"issue"}],
 "orders":[["created_at",true]],"limit":20,"as_of":null}
→ 200 {"result":[{"card_id":"c1","issue":"pty tmux"}]}
```

**count**:

```jsonc
{"op":"count","table":"cards","predicates":[{"op":"in_","column":"card_id","value":["c1","c2"]}]}
→ 200 {"result": 2}
```

**insert**(单条或批量都放 `rows`):

```jsonc
{"op":"insert","table":"cards","rows":[{"card_id":"c1","issue":"pty tmux","kind":"issue"}]}
→ 200 {"result": null}
```

**delete**(打墓碑,返回受影响行数):

```jsonc
{"op":"delete","table":"cards","predicates":[{"op":"eq","column":"card_id","value":"c1"}]}
→ 200 {"result": 1}
```

**sql**(只读):

```jsonc
{"op":"sql","statement":"SELECT kind, count(*) AS n FROM cards GROUP BY kind","as_of":null}
→ 200 {"result":[{"kind":"issue","n":3}]}
```

非 `SELECT/WITH` → `400 {"error":{"type":"ReadOnlyError","message":"…"}}`。

## 时光机与只读闸(`as_of`)

- `as_of` 非 null(ISO-8601)→ 该请求**只读回退**到那个时刻:只看得见当时存在的行。
- 写类 `op`(`insert`/`delete`/`rebuild`/`vacuum`)带 `as_of` → `400` `ReadOnlyError`(权威判定在 server 端)。
- `as_of` 是 **per-request** 的:一个 server 能同时服务各自 `as_of` 的多个客户端。
- `[M4]` 当前 `sql` 尚未对 `as_of` 回退(需 as-of 视图注册);ORM 的 `select`/`count` 已回退。

## 错误 → HTTP 状态码

| 异常类型 | 状态码 |
|---|---|
| `NotSupportedYet` | `501` |
| `SeekbaseUnavailable` | `503` |
| `SchemaError` / `EmbedderInvalid` / `ReadOnlyError` / `QueryError` / 其它 `SeekbaseError` | `400` |
| 鉴权失败 | `401`(`type: "Unauthorized"`) |
| 未预期内部异常 | `500`(`type: "Internal"`) |

层级与语义见 [errors.md](errors.md)。
