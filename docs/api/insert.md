# Insert API

写数据。**同步**:提交要写的行,seekbase 内联 embed + jieba 分词、把行(含向量)一次写入 files → DuckDB → 检索索引,**响应返回时已全部落定**,带回的 task **出生即 done**(写回执,[../works/task.md §2](../works/task.md))。**主键写一次**:重复主键报错(`QueryError`),整批拒。

**只增**:没有 update / upsert;「改」= 追加新行(旧行由 [delete](delete.md) 打墓碑)。

**函数形态**:

```python
task_id = await db.insert("cards", [{"card_id": "c1", "issue": "pty tmux", "kind": "issue"}])
st = await db.wait(task_id)          # 写是同步的:立即返回,state 已是 done
```

---

## POST /v1/insert — 提交

追加一行或多行,都放 `rows`。同步落库后返回。

### 请求体

```json
{
  "table": "cards",
  "rows": [
    {"card_id": "c1", "issue": "pty tmux", "kind": "issue"}
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `table` | 是 | 目标表 |
| `rows` | 是 | 行对象数组;键须是声明列,未知列 → `QueryError`;缺的列填 `NULL` |

### 响应

```json
{"task": "tk_20260722_ab12cd34ef56", "op": "insert", "state": "done",
 "submitted_at": "…", "finished_at": "…"}
```

`200 OK`。`state` 到手即 `done`——**写完立刻可被 query / search 读到**(read-your-write)。状态复查走 [tasks.md](tasks.md)(`GET /v1/tasks/{id}`;`GET /v1/writes/{ticket}` 兼容别名)。

### 副作用

写入按 files → 行 → 索引的顺序同步落地(见 [`../works/store.md`](../works/store.md)):**文件最先** append 进 `ds=今天/<表>.jsonl`(canonical),再往 DuckDB INSERT(业务列 + `ds`/`created_at`;vss 后端连 `_vec_<列>`/`_tok_<列>` 一起随行写并重建 FTS,lance 后端追加进侧数据集)。任一步崩溃可从文件 `rebuild`/校准。

### 错误

| 情况 | 状态 / type |
|---|---|
| 未知表 | 400 `SchemaError` |
| 未知列 / 批内或既有主键重复(写一次) | 400 `QueryError` |
| `rows` 为空 / 非数组 | 400 `QueryError` |
