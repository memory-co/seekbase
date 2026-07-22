# Admin API

管理动作:从文件重建派生层、健康探活。`rebuild` 是**真后台 task**([../works/task.md §3](../works/task.md)):立即返回 pending,重放在后台跑,轮询到 `done + stats`。

> **没有 vacuum / 物理删**:`delete` 永远只是 `deleted_ds` 墓碑,历史**永久保留**(文件真·纯 append,一次都不回改)。这是刻意的——memory 系统里历史本身是资产。

---

## POST /v1/rebuild — 从文件重建(后台 task)

按 `ds` 顺序 replay 全部 `<表>.jsonl` → 清空并重灌 DuckDB(重新 embed + 重建检索索引,vss / lance 后端都覆盖)。「表丢了能从文件重建」的内建动作(见 [`../works/store.md`](../works/store.md))。

**函数形态**:`task_id = await db.rebuild(); st = await db.wait(task_id)`

### 请求体 / 响应

```json
{}   →   200 {"task": "tk_20260722_…", "op": "rebuild", "state": "pending", "submitted_at": "…"}
```

立即返回;用 [tasks.md](tasks.md) 轮询:

```json
GET /v1/tasks/{id}   →   200 {"task": "…", "op": "rebuild", "state": "done",
                              "stats": {"tables": 2, "rows": 120, "tombstones": 5}, …}
```

失败记 `failed + error`;后台 task 有 max runtime(默认 300s)。

---

## GET /v1/health — 健康

```
GET /v1/health   →   200 {"ready": true}
```

**函数形态**:`db.ready`。`ready=false` → 宿主应回 503 / 降级。
