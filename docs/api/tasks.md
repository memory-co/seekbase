# Tasks API

统一操作句柄([../works/task.md](../works/task.md)):写回执(出生即 done)、rebuild(真后台)、慢查询(`as_task` / `wait_ms` 超时升级)共用一个 id 体系(`tk_<ds>_<hex>`,ds 自定位)和这组端点。

**函数形态**:`db.tasks()` / `db.task_status(id)` / `db.task_result(id)` / `db.cancel_task(id)` / `db.wait(id)`。

## task 对象(wire)

```json
{"task": "tk_20260722_ab12cd34ef56", "op": "query", "state": "done",
 "error": null, "query": "search cards '…' | SELECT …", "rows": 10,
 "submitted_at": "…", "finished_at": "…"}
```

| 字段 | 说明 |
|---|---|
| `task` | id(旧字段名 `ticket` 在入参侧兼容) |
| `op` | `insert` / `delete` / `rebuild` / `query` |
| `state` | `pending` / `running` / `done` / `failed` / `cancelled`(写恒 `done` 到手) |
| `error` | `failed` 时的错误信息 |
| `matched` | delete:软删命中数 |
| `stats` | rebuild:`{tables, rows, tombstones}` |
| `query` / `rows` | op=query:SPL 文本 / 结果行数(**表只记 query,结果在文件**) |

## GET /v1/tasks — 最近列表

```
200 {"tasks": [ {task…}, … ]}          // 新在前,末态呈现;固定窗口(50)
```

## GET /v1/tasks/{id} — 单个状态

```
200 {task…}                            // 未知 id → 404 NotFound
```

`GET /v1/writes/{ticket}` 是本端点的**兼容别名**(ticket = 出生即 done 的 task)。

## GET /v1/tasks/{id}/result — 后台查询的结果行

```
200 {"rows": [ … ]}
```

| task 状态 | 行为 |
|---|---|
| `done`(op=query) | 返回行(从结果文件 `tasks/results/<id>.jsonl` 读回) |
| `pending` / `running` | 400 `QueryError`(还没完) |
| `failed` / `cancelled` | 400 `QueryError`(带原错误) |
| 结果文件已过保留期 GC(默认 7 天) | 400 `QueryError`(结果过期;记录本身保 30 天) |
| op ≠ query | 400 `QueryError`(写 / rebuild 没有结果行) |

## POST /v1/tasks/{id}/cancel — 取消

```
POST {}   →   200 {task…}              // state 转 cancelled(若已终态则原样返回)
```

**诚实边界**:取消保证「结果不再交付、状态可见」;已在 server 读线程上执行中的 SQL 段不被中断(跑完即弃),server 关闭时对全部 cursor `interrupt()` 兜底。后台 task 另有 max runtime(默认 300s → `failed`)。
