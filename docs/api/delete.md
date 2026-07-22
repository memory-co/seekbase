# Delete API

删数据。**同步**,同 [insert](insert.md):提交删除条件,软删匹配的行,返回**出生即 done 的 task**。删除是**软删**——只标 `deleted_ds` / `deleted_at`,行永久留着。

**打墓碑,非物理删**:行物理还在(时光机仍能回到删除前),`query` 默认自动滤掉、`search` 段的候选谓词一并裁掉。**没有物理删**——墓碑永久保留(历史即资产)。

**函数形态**:

```python
task_id = await db.delete("cards", where="card_id = ?", params=["c1"])
print((await db.wait(task_id)).matched)      # 软删命中数
```

---

## POST /v1/delete — 提交

给匹配 `where` 的存活行打墓碑,同步落定后返回。

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
| `where` | 是 | 布尔条件(SQL 片段);**必须给**——不接受无条件全表删 |
| `params` | 否 | 位置参数,填充 `where` 里的 `?`(参数绑定,防注入) |

### 响应

```json
{"task": "tk_20260722_9f3ab1c2d4e5", "op": "delete", "state": "done",
 "matched": 1, "submitted_at": "…", "finished_at": "…"}
```

`200 OK`;`matched` = 打了墓碑的行数(已是墓碑的行不重复打)。

### 副作用

canonical 文件在**删除日分区**追加一条 `{"_deleted": pk, …}` 墓碑记录;派生 DuckDB 对该行 `UPDATE deleted_ds/deleted_at`(软删)。检索索引**不动**——软删行留在索引里,查询时靠 as-of 谓词裁掉(时光机回到删除前照样搜得到,见 [`../works/time_machine.md`](../works/time_machine.md))。

### 错误

| 情况 | 状态 / type |
|---|---|
| 缺 `where`(拒绝全表删) | 400 `QueryError` |
| 未知表 | 400 `SchemaError` |
| 未知列、`where` 语法错 | 400 `QueryError` |
