# Delete API

删数据。**异步**,同 [insert](insert.md) 的提交 + 轮询模式:提交删除条件,返回 `ticket`,用 [`GET /v1/writes/{ticket}`](insert.md#get-v1writesticket--查状态) 查状态。

**打墓碑,非物理删**:`delete` 唯一语义是给匹配的存活行写 `deleted_at`。行物理还在(时光机 / raw SQL 仍能看到「它曾存在」),`query` 默认自动滤掉。**没有物理删**——墓碑永久保留(历史即资产)。

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

- 记一条带 `ds`(删除日)的**墓碑事件**——canonical 文件在**删除日分区追加**一条 `{"_deleted": pk, …}`,派生 DuckDB **INSERT 一条 del 事件**(纯 append,**不回改任何已写的行**)。见 [`../works/store.md` §5](../works/store.md)。时光机据事件重放,`ds_end` 早于删除日仍见该行(那时最新事件还是 put)。
- 已经是墓碑的行不再重复打。

### 错误

| 情况 | 状态 / type |
|---|---|
| 缺 `where`(拒绝全表删) | 400 `QueryError` |
| 未知表 / 列、`where` 语法错 | 400 `QueryError` |

---

## M1 现状

- 提交 / 查状态:✅ 可用,同步兑现(即返 `done` 的 ticket)。
- 文件镜像里往删除日 `<表>.jsonl` 追加墓碑记录:`[M2]`。
