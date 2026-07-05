# Admin API

管理动作:从文件重建派生层、丢历史、健康探活。`rebuild` / `vacuum` 是**异步写**,同 [insert](insert.md) 返回 `ticket`、用 [`GET /v1/writes/{ticket}`](insert.md#get-v1writesticket--查状态) 轮询。

---

## POST /v1/rebuild — 从文件重建 ✅

按 `ds` 顺序 replay 全部 `<表>.jsonl` → 重灌 DuckDB(向量侧 M3)。「表丢了能从文件重建」的内建动作(见 [`../works/store.md`](../works/store.md))。异步,返回 `ticket`;`done` 后带 `stats`。

**函数形态**:`ticket = await db.rebuild(); st = await db.wait(ticket)`

### 请求体 / 响应

```json
{}   →   200 {"ticket": "wr_…", "op": "rebuild", "state": "done",
              "stats": {"tables": 2, "rows": 120, "tombstones": 5}}
```

---

## POST /v1/vacuum — 丢历史 ✅

**按行**物理清 `deleted_ds < before` 的**死行**(DuckDB 行 + 文件里那些行的全部事件 + 向量)。**唯一真正物理删的动作**,明说这是在丢历史;**不是**整块删分区(活行、删于 `≥ before` 的行都保留)。见 [`../works/time_machine.md` §8](../works/time_machine.md)。异步,返回 `ticket`。

**函数形态**:`ticket = await db.vacuum(before="20260601"); st = await db.wait(ticket)`

### 请求体

```json
{"before": "20260601"}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `before` | 是 | `YYYYMMDD`;清掉 `deleted_ds < before` 的死行 |

### 响应

```json
200 {"ticket": "wr_…", "op": "vacuum", "state": "done", "stats": {"purged": 3}}
```

### 错误

| 情况 | 状态 / type |
|---|---|
| 缺 `before` / 非 `YYYYMMDD` | 400 `QueryError` |

---

## GET /v1/health — 健康

```
GET /v1/health   →   200 {"ready": true}
```

**函数形态**:`db.ready`。`ready=false` → 宿主应回 503 / 降级。
