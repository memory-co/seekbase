# Admin API

管理动作:从文件重建派生层、健康探活。`rebuild` 是**异步写**,同 [insert](insert.md) 返回 `ticket`、用 [`GET /v1/writes/{ticket}`](insert.md#get-v1writesticket--查状态) 轮询。

> **没有 vacuum / 物理删**:`delete` 永远只是 `deleted_ds` 墓碑,历史**永久保留**(文件真·纯 append,一次都不回改)。这是刻意的——memory 系统里历史本身是资产。

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

## GET /v1/health — 健康

```
GET /v1/health   →   200 {"ready": true}
```

**函数形态**:`db.ready`。`ready=false` → 宿主应回 503 / 降级。
