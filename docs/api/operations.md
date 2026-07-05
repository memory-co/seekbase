# Operations

每个操作 = `POST /v1/execute` 里一个 `op`。信封字段全集、谓词/排序编码、鉴权、错误码见 [README.md](README.md)。本页逐个操作给**请求体 / 响应 / 副作用 / 错误**,以及对应的**函数形态**一行。

---

## op: select — 读行

按条件取行。`columns` 省略 = 声明列 + `created_at`。默认自动滤掉墓碑行;带 `as_of` 则回退到那个时刻。

**函数形态**:`await db.table("cards").select("card_id","issue").eq("kind","issue").order("created_at", desc=True).limit(20)`

### 请求体

```json
{
  "op": "select",
  "table": "cards",
  "columns": ["card_id", "issue"],
  "predicates": [{"op": "eq", "column": "kind", "value": "issue"}],
  "orders": [["created_at", true]],
  "limit": 20, "offset": null,
  "as_of": null
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `table` | 是 | 目标表 |
| `columns` | 否 | 投影;空 = 声明列 + `created_at` |
| `predicates` | 否 | 过滤(编码见 README);未知列 → `QueryError` |
| `orders` | 否 | `[[列, desc], …]` |
| `limit` / `offset` | 否 | 分页 |
| `as_of` | 否 | 非 null → 只读回退 |

### 响应

```json
{"result": [{"card_id": "c1", "issue": "pty tmux"}]}
```

`result` 是行数组,每行一个对象。

### 错误

| 情况 | 状态 / type |
|---|---|
| 未知表 / 列 | 400 `QueryError` |

---

## op: count — 计数

匹配谓词的行数。语义同 `select`,只回数字。

**函数形态**:`await db.table("cards").in_("card_id", ["c1","c2"]).count()`

### 请求体

```json
{"op": "count", "table": "cards",
 "predicates": [{"op": "in_", "column": "card_id", "value": ["c1", "c2"]}],
 "as_of": null}
```

### 响应

```json
{"result": 2}
```

### 错误

| 情况 | 状态 / type |
|---|---|
| 未知表 / 列 | 400 `QueryError` |

---

## op: insert — 追加(只增)

追加一行或多行,都放 `rows`。引擎 insert-only:没有 update/upsert。`created_at` 自动写(也可自带),`deleted_at` 置空。

**函数形态**:`await db.table("cards").insert({"card_id":"c1","issue":"pty tmux","kind":"issue"})`(或传 `list[dict]` 批量)

### 请求体

```json
{"op": "insert", "table": "cards",
 "rows": [{"card_id": "c1", "issue": "pty tmux", "kind": "issue"}]}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `table` | 是 | 目标表 |
| `rows` | 是 | 行对象数组;键须是声明列,未知列 → `QueryError` |

### 响应

```json
{"result": null}
```

### 副作用

- 校验列名 → 失败整条不落库。
- 写 DuckDB 行(`created_at` 自动)。`[M2/M3]` 后续:先落文件镜像、再入 outbox 兑现向量(见 [`../works/store.md`](../works/store.md))。

### 错误

| 情况 | 状态 / type |
|---|---|
| 未知列 | 400 `QueryError` |
| `as_of` 非 null(往过去写) | 400 `ReadOnlyError` |

---

## op: delete — 打墓碑(非物理删)

`delete` 唯一语义是给匹配的**存活**行写 `deleted_at`。行物理还在(raw SQL 能看到),正常查询看不到。返回受影响行数。

**函数形态**:`await db.table("cards").delete().eq("card_id", "c1")`

### 请求体

```json
{"op": "delete", "table": "cards",
 "predicates": [{"op": "eq", "column": "card_id", "value": "c1"}]}
```

### 响应

```json
{"result": 1}
```

`result` = 打了墓碑的行数。

### 副作用

- 给匹配的存活行写 `deleted_at`(唯一一次引擎代管的重写)。`[M2]` 文件镜像里也写回 `deleted_at` / 追加墓碑记录。

### 错误

| 情况 | 状态 / type |
|---|---|
| 未知表 / 列 | 400 `QueryError` |
| `as_of` 非 null | 400 `ReadOnlyError` |

---

## op: search — 语义检索 `[M3]`

在一条链上混语义检索与结构化过滤:`search(text)` 自动 embed + 向量检索,与谓词组合,返回带 `_score` 的行。

**函数形态**:`await db.table("cards").search("pty tmux").eq("kind","issue").limit(10)`

### 请求体

```json
{"op": "search", "table": "cards",
 "predicates": [{"op": "eq", "column": "kind", "value": "issue"}],
 "limit": 10, "as_of": null}
```

> 文本本身在函数形态由 `search(text)` 提供;线格式的 `search` 载荷随 M3 定形。

### 响应(目标)

```json
{"result": [{"card_id": "c1", "issue": "pty tmux", "_score": 0.83}]}
```

### 错误

| 情况 | 状态 / type |
|---|---|
| M1 未实现 | 501 `NotSupportedYet` |

---

## op: sql — 只读直查

只读逃生舱:join / 聚合 / 窗口 / 对账。语句须以 `SELECT` / `WITH` 开头,否则拒绝——写只能走 ORM。

**函数形态**:`await db.sql("SELECT kind, count(*) AS n FROM cards GROUP BY kind")`

### 请求体

```json
{"op": "sql", "statement": "SELECT kind, count(*) AS n FROM cards GROUP BY kind", "as_of": null}
```

### 响应

```json
{"result": [{"kind": "issue", "n": 3}]}
```

### 错误

| 情况 | 状态 / type |
|---|---|
| 非 `SELECT`/`WITH` 语句 | 400 `ReadOnlyError` |

> `[M4]` `as_of` 尚未对 raw SQL 回退(需 as-of 视图注册);ORM 的 `select`/`count` 已回退。

---

## op: flush — 读己之写 `[M3]`

排干 outbox,让刚写入的行对 `search()` 立即可见。结构化查询本就强一致、不需 flush。

**函数形态**:`await db.flush()`

### 请求体 / 响应

```json
{"op": "flush"}   →   {"result": null}
```

M1 为 no-op(向量引擎/outbox 尚未落地);接口先在,契约稳定。

---

## op: rebuild — 从文件重建 `[M2]`

通读 `files` 声明的全部文件 → 重灌 DuckDB + LanceDB。「表丢了能从文件重建」的内建动作(见 [`../works/store.md`](../works/store.md))。

**函数形态**:`await db.rebuild()`

### 请求体 / 响应

```json
{"op": "rebuild"}   →   {"result": null}
```

### 错误

| 情况 | 状态 / type |
|---|---|
| M1 未实现 | 501 `NotSupportedYet` |

---

## op: vacuum — 显式丢历史 `[M4]`

物理清 `before` 之前的墓碑(行 + 向量 + 文件)。唯一会真正物理删的动作,**明说这是在丢历史**。时光机连接下不可用。

**函数形态**:`await db.vacuum(before="2026-06-01T00:00:00Z")`

### 请求体 / 响应

```json
{"op": "vacuum", "before": "2026-06-01T00:00:00Z"}   →   {"result": null}
```

### 错误

| 情况 | 状态 / type |
|---|---|
| M1 未实现 | 501 `NotSupportedYet` |

---

## GET /v1/health — 健康

```
GET /v1/health   →   200 {"ready": true}
```

**函数形态**:`db.ready`。`ready=false` → 宿主应回 503 / 降级。
