# Delete API

删数据。**异步**,同 [insert](insert.md) 的提交 + 轮询模式:提交删除条件,返回 `ticket`,用 [`GET /v1/writes/{ticket}`](insert.md#get-v1writesticket--查状态) 查状态。

**打墓碑,非物理删**:`delete` 唯一语义是给匹配的存活行写 `deleted_at`。行物理还在(时光机 / raw SQL 仍能看到「它曾存在」),`query` 默认自动滤掉。真正物理删只有 [`vacuum`](admin.md)。

**函数形态**:

```python
ticket = await db.delete("cards", where="card_id = ?", params=["c1"])
await db.wait(ticket)
```

---

## POST /v1/delete — 提交

给匹配 `where` 的存活行打墓碑。立即返回 `ticket`,不等落盘。

### 请求体

```json
{
  "table": "cards",
  "where": "card_id = ?",
  "params": ["c1"]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `table` | 是 | 目标表 |
| `where` | 是 | 布尔条件(SQL 片段,同 [query](query.md) 的谓词);**必须给**——不接受无条件全表删 |
| `params` | 否 | 位置参数,填充 `where` 里的 `?`(参数绑定,防注入) |

### 响应

```json
{"ticket": "wr_01jzp3nq", "state": "pending"}
```

`202 Accepted`。状态查询见 [insert.md](insert.md#get-v1writesticket--查状态);`done` 后 `matched` 给出打了墓碑的行数:

```json
{"ticket": "wr_01jzp3nq", "op": "delete", "state": "done", "matched": 1, "error": null}
```

### 副作用

- 记一条带 `ds`(删除日)的**墓碑**:canonical 文件在**当天分区追加**一条删除记录、**不回改原文件**;派生的 DuckDB 行一并置 `deleted_at`(见 [`../works/store.md` §5](../works/store.md))。时光机据分区裁剪,as-of 早于删除日仍见该行。
- 已经是墓碑的行不再重复打。

### 错误

| 情况 | 状态 / type |
|---|---|
| 缺 `where`(拒绝全表删) | 400 `QueryError` |
| 未知表 / 列、`where` 语法错 | 400 `QueryError` |

---

## M1 现状

- 提交 / 查状态:✅ 可用,同步兑现(即返 `done` 的 ticket)。
- 文件镜像里的墓碑写回:`[M2]`。
