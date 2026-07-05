# Insert API

写数据。**异步**:提交要写的行,拿回一个 `ticket`,不阻塞等落盘;再用状态接口按 `ticket` 轮询这次写入什么时候真正兑现(files → 行 → 向量)。

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
{"ticket": "wr_01jz8k2m", "state": "pending"}
```

`202 Accepted`。`ticket` 用于查状态;`state` 初始 `pending`。

### 副作用

写入按 files → 行 → 向量的顺序兑现(见 [`../works/store.md`](../works/store.md)):文件最先原子落地,再一个 DuckDB 事务写行 + 入队,最后异步补向量。任一步崩溃可从文件校准。

### 错误

| 情况 | 状态 / type |
|---|---|
| 未知表 / 列 | 400 `QueryError` |
| `rows` 为空 / 非数组 | 400 `QueryError` |

---

## GET /v1/writes/{ticket} — 查状态

按 `ticket` 查这次写入(insert / delete / rebuild / vacuum 都用这个)。

### 响应

```json
{"ticket": "wr_01jz8k2m", "op": "insert", "state": "done", "error": null}
```

| 字段 | 说明 |
|---|---|
| `state` | `pending`(兑现中) / `done`(已落盘、可被 query/search 读到) / `failed` |
| `error` | `failed` 时的错误信息,否则 `null` |

- **读己之写**:提交后 query/search 不保证立刻看到这次写入;等 `state` 到 `done` 再读。
- 幂等:同一 `ticket` 可反复查。

### 错误

| 情况 | 状态 / type |
|---|---|
| `ticket` 不存在 | 404 `NotFound` |

---

## M1 现状

- 提交 / 查状态接口:✅ 可用。
- **写目前同步兑现**(outbox + 向量在 `[M3]`、文件镜像在 `[M2]`):当前 `insert` 落库后即返回 `state: "done"` 的 ticket——异步骨架先立住,真正的 files/向量异步兑现随 M2/M3 接上,接口不变。
