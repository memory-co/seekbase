# Query API

读接口:**传一段 SQL,拿回行**。结构化查询、语义检索、时光机都在这一个接口里——

- **结构化**:普通 `SELECT`(join / 聚合 / 窗口都行)。
- **语义检索**:SQL 里用 `search('文本')` 函数,自动 embed + 向量检索,暴露 `_score` 列,和结构化过滤写在同一条 SQL 里(**不单独开搜索接口**)。
- **时光机**:请求参数 `as_of` 给一个时刻,整条查询回退到那时的世界。

只读:语句须是 `SELECT` / `WITH`——写走 [insert.md](insert.md) / [delete.md](delete.md)。schema / embedder 见 [setup.md](setup.md)。

**函数形态**:`await db.query("SELECT card_id, issue FROM cards WHERE kind = ?", params=["issue"], as_of=None)`

---

## POST /v1/query

### 请求体

```json
{
  "sql": "SELECT card_id, issue FROM cards WHERE kind = ? ORDER BY created_at DESC LIMIT 20",
  "params": ["issue"],
  "as_of": null
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `sql` | 是 | 一条只读语句(`SELECT` / `WITH` 开头);非只读 → `ReadOnlyError` |
| `params` | 否 | 位置参数,填充 `sql` 里的 `?`(参数绑定,防注入);默认 `[]` |
| `as_of` | 否 | ISO-8601 时刻;非 null → 整条查询只见那时及之前存在的行(时光机) |

### 响应

```json
{
  "rows": [
    {"card_id": "c1", "issue": "pty tmux"}
  ]
}
```

- `rows` 是行数组,列由 `sql` 的投影决定。
- 墓碑行(`deleted_at` 非空)默认自动滤掉;`as_of` 下按那个时刻的存活判定。

### 错误

| 情况 | 状态 / type |
|---|---|
| 语句非 `SELECT` / `WITH` | 400 `ReadOnlyError` |
| 未知表 / 列、SQL 语法错 | 400 `QueryError` |
| `as_of` 非 ISO-8601 | 400 `QueryError` |
| `search()` 用在无 `searchable` 列的表上 | 400 `QueryError` |

---

## `search()` — SQL 里的语义检索

`search('文本')` 是查询里的一个函数,不是另一个接口。出现它时,seekbase 自动:① 用注入的 embedder 把文本变向量;② 到该表的向量侧检索;③ 与 SQL 其余谓词组合;④ 暴露一个 `_score` 列(相似度)。

```json
{
  "sql": "SELECT card_id, issue, _score FROM cards WHERE search('为什么 pty 会让人想到 tmux') AND kind = 'issue' ORDER BY _score DESC LIMIT 10"
}
```

```json
{
  "rows": [
    {"card_id": "c1", "issue": "pty vs tmux", "_score": 0.83}
  ]
}
```

- **在 `WHERE` 里**:把结果限定为语义命中的行;结构化谓词(`kind = 'issue'`)下推到向量检索里,保「先过滤后取 top-k」。
- **`_score` 列**:相似度,可在 `SELECT` / `ORDER BY` 里用;不带 `search()` 的查询没有这一列。
- 一张表只有声明了 `searchable` 列(见 [setup.md](setup.md))才能被 `search()`;否则 `QueryError`。
- **调用方永远不见向量、不算 embedding**——只写文本。

> **一致性**:向量侧最终一致,`search()` 可能滞后于刚提交的写入(通常毫秒级);要读己之写,等这次写入的 ticket 到 `done`(见 [insert.md](insert.md))。结构化查询(不带 `search()`)永远强一致。

---

## 时光机 `as_of`

`as_of` 给一个 ISO-8601 时刻,整条查询回退到那时:只见那时刻及之前建、且当时还没删的行。`search()` 也一并回退——检索的是「当时存在的向量」。

```json
{"sql": "SELECT * FROM cards WHERE kind = 'issue'", "as_of": "2026-06-01T00:00:00Z"}
```

- 一个 server 能同时服务各自 `as_of` 的多个请求(`as_of` 是 per-request 的)。

---

## M1 现状

- 普通结构化 SQL 查询:✅ 可用。
- `search()`:`[M3]` 向量引擎落地前调用 → `501 NotSupportedYet`。
- `as_of` 对 raw SQL 的回退:`[M4]` 需 as-of 视图注册,当前直查看到的是当前态(ORM 侧的等值/范围查询已回退)。
