# Insert API

写数据。**同步**:提交要写的行,seekbase 内联 embed + jieba 分词、把行(含向量)一次写入 files → DuckDB,`ticket` 返回即 `done`。**主键写一次**:重复主键报错(`QueryError`)。

**只增**:没有 update / upsert;「改」= 追加新行(旧行由 [delete](delete.md) 打墓碑)。

**函数形态**:

```python
ticket = await db.insert("cards", [{"card_id": "c1", "issue": "pty tmux", "kind": "issue"}])
st = await db.write_status(ticket)     # 轮询一次
await db.wait(ticket)                   # 或阻塞到 done / failed
```

---

## POST /v1/insert — 提交

追加一行或多行,都放 `rows`。立即返回 `ticket`,不等落盘。

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
| `rows` | 是 | 行对象数组;键须是声明列,未知列 → `QueryError`;`created_at` 自动写,也可自带 |

### 响应

```json
{"ticket": "wr_01jz8k2m", "state": "done"}
```

`200 OK`。`ticket` 用于查状态;`state` 立即为 `done`(写已落库)。

### 副作用

写入按 files → 行的顺序同步落地(见 [`../works/store.md`](../works/store.md)):**文件最先** append 进 `ds=今天/<表>.jsonl`(canonical),再往 DuckDB **INSERT 一行**——业务列 + `ds`/`created_at` + 每个 searchable 列的 `_vec_<列>`(inline embed)/`_tok_<列>`(jieba),并同步重建该表 FTS 索引。任一步崩溃可从文件 `rebuild`/校准。

### 错误

| 情况 | 状态 / type |
|---|---|
| 未知表 / 列 | 400 `QueryError` |
| `rows` 为空 / 非数组 | 400 `QueryError` |

---

## GET /v1/writes/{ticket} — 查状态

按 `ticket` 查这次写入(insert / delete / rebuild 都用这个)。

### 响应

```json
{"ticket": "wr_01jz8k2m", "op": "insert", "state": "done", "error": null}
```

| 字段 | 说明 |
|---|---|
| `state` | `done`(已落库、可被 query/search 读到) / `failed` |
| `error` | `failed` 时的错误信息,否则 `null` |

- **读己之写**:提交后 query/search 不保证立刻看到这次写入;等 `state` 到 `done` 再读。
- 幂等:同一 `ticket` 可反复查。

### 错误

| 情况 | 状态 / type |
|---|---|
| `ticket` 不存在 | 404 `NotFound` |

---

## 现状

- 提交 / 查状态接口:✅ 可用。
- **文件镜像(M2)✅**:`insert` 先 append 进 `<表>.jsonl`,再写 DuckDB 行;`rebuild` 能从文件重灌(见 [admin.md](admin.md))。
- **检索侧同步 ✅**:`insert` 内联 embed + jieba 分词,把 searchable 列的 `_vec`/`_tok` 随行写入(向量一次写定、永不 UPDATE),并同步 `create_fts_index(overwrite=1)` 重建 FTS;`insert` 返回即 `search()` 能搜到,`ticket` 立即 `done`。
