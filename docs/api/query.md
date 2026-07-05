# Query API

读接口:**传一段 SQL,拿回行**。结构化查询、语义检索、时光机都在这一个接口里——

- **结构化**:普通 `SELECT`(join / 聚合 / 窗口都行)。
- **语义检索**:SQL 里用 `search('文本')` 函数,自动 embed + 向量检索,暴露 `_score` 列,和结构化过滤写在同一条 SQL 里(**不单独开搜索接口**)。
- **时间窗 / 时光机**:请求参数 `ds_start` / `ds_end` 按日期分区圈定时间窗——只给 `ds_end` = 回到那天(时光机),两个都给 = 查一个时间段。

只读:语句须是 `SELECT` / `WITH`——写走 [insert.md](insert.md) / [delete.md](delete.md)。schema / embedder 见 [setup.md](setup.md)。

**函数形态**:`await db.query("SELECT card_id, issue FROM cards WHERE kind = ?", params=["issue"], ds_end="20260601")`

---

## POST /v1/query

### 请求体

```json
{
  "sql": "SELECT card_id, issue FROM cards WHERE kind = ? ORDER BY created_at DESC LIMIT 20",
  "params": ["issue"],
  "ds_start": null,
  "ds_end": null
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `sql` | 是 | 一条只读语句(`SELECT` / `WITH` 开头);非只读 → `ReadOnlyError` |
| `params` | 否 | 位置参数,填充 `sql` 里的 `?`(参数绑定,防注入);默认 `[]` |
| `ds_start` | 否 | 日期 `YYYYMMDD`,闭区间下界;只读 `ds >= ds_start` 的分区 |
| `ds_end` | 否 | 日期 `YYYYMMDD`,闭区间上界;只读 `ds <= ds_end` 的分区。只给它 = 时光机(见下) |

### 响应

```json
{
  "rows": [
    {"card_id": "c1", "issue": "pty tmux"}
  ]
}
```

- `rows` 是行数组,列由 `sql` 的投影决定。
- 墓碑行(`deleted_at` 非空)默认自动滤掉;带时间窗时按分区(`ds`)裁剪后的存活判定。

### 错误

| 情况 | 状态 / type |
|---|---|
| 语句非 `SELECT` / `WITH` | 400 `ReadOnlyError` |
| 未知表 / 列、SQL 语法错 | 400 `QueryError` |
| `ds_start` / `ds_end` 非 `YYYYMMDD` | 400 `QueryError` |
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

## 时间窗 `ds_start` / `ds_end`(日期分区)

每行带一个引擎代管的分区列 `ds`(写入日,`YYYYMMDD`)。`ds_start` / `ds_end` 是**闭区间**的两端,直接就是完整的分区过滤语义——传哪个决定语义:

| 传入 | 语义 | 创建过滤(`ds`) |
|---|---|---|
| 都不传 | 全部(当前态) | 无 |
| 只 `ds_end` | **时光机**:回到那天及之前 | `ds <= ds_end` |
| 只 `ds_start` | 那天及之后、至今仍活 | `ds >= ds_start` |
| 都传 | 一个时间段 | `ds_start <= ds <= ds_end` |

```json
{"sql": "SELECT * FROM cards WHERE kind = 'issue'", "ds_end": "20260601"}          // 时光机:6/1 及之前
{"sql": "SELECT * FROM cards", "ds_start": "20260601", "ds_end": "20260607"}        // 6/1~6/7 一周
{"sql": "SELECT * FROM cards", "ds_start": "20260601"}                              // 6/1 之后至今
```

> 上表是**创建维度**的过滤;存活判定还叠加**删除 horizon**——as-of `ds_end` 会把「删于 `ds_end` 之后」的行仍算作可见(靠 `deleted_ds` 字段)。完整可见性谓词 `ds <= D AND (deleted_ds IS NULL OR deleted_ds > D)`、四种情况的精确语义、以及完备性证明见 [`../works/time_machine.md`](../works/time_machine.md)。

- 这是**分区裁剪**,扫描量随时间窗收敛;`search()` 一并按 `ds` 裁剪。
- 等价于在 SQL 里写 `WHERE ds >= … AND ds <= …`;`ds_start`/`ds_end` 是把它提成请求参数(server 直接用来选分区)。也可在 `sql` 里自己用 `ds` 列(`WHERE ds = '20260605'` 看某天)。
- 粒度到**天**(离线大数据惯例);日内更细由 `created_at` 列做二级过滤。
- per-request:一个 server 能同时服务各自时间窗的多个请求。

---

## M1 现状

- 普通结构化 SQL 查询:✅ 可用。
- `search()`:`[M3]` 向量引擎落地前调用 → `501 NotSupportedYet`。
- `ds` 日期分区与 `ds_start`/`ds_end` 裁剪:`[M4]`,当前未分区、看到的是当前态。
