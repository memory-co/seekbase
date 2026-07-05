# Admin API

管理动作:从文件重建派生层、丢历史、健康探活。`rebuild` / `vacuum` 是**异步写**,同 [insert](insert.md) 返回 `ticket`、用 [`GET /v1/writes/{ticket}`](insert.md#get-v1writesticket--查状态) 轮询。

---

## POST /v1/rebuild — 从文件重建 `[M2]`

按 `ds` 顺序 replay 全部 `<表>.jsonl` → 重灌 DuckDB + LanceDB。「表丢了能从文件重建」的内建动作(见 [`../works/store.md`](../works/store.md))。异步,返回 `ticket`。

**函数形态**:`ticket = await db.rebuild(); await db.wait(ticket)`

### 请求体 / 响应

```json
{}   →   202 {"ticket": "wr_…", "state": "pending"}
```

### 错误

| 情况 | 状态 / type |
|---|---|
| M1 未实现 | 501 `NotSupportedYet` |

---

## POST /v1/vacuum — 丢历史 `[M4]`

物理清 `before` 之前的墓碑(行 + 向量 + 文件)。**唯一会真正物理删的动作**,明说这是在丢历史。异步,返回 `ticket`。

**函数形态**:`ticket = await db.vacuum(before="2026-06-01T00:00:00Z"); await db.wait(ticket)`

### 请求体

```json
{"before": "2026-06-01T00:00:00Z"}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `before` | 是 | ISO-8601;清掉 `deleted_at < before` 的墓碑 |

### 响应

```json
202 {"ticket": "wr_…", "state": "pending"}
```

### 错误

| 情况 | 状态 / type |
|---|---|
| 缺 `before` / 非 ISO-8601 | 400 `QueryError` |
| M1 未实现 | 501 `NotSupportedYet` |

---

## GET /v1/health — 健康

```
GET /v1/health   →   200 {"ready": true}
```

**函数形态**:`db.ready`。`ready=false` → 宿主应回 503 / 降级。
