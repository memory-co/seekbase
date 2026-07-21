# 操作句柄:`db.tasks` / `task_status` / `task_result` / `cancel_task` / `rebuild`

一切「会完成的操作」都是一条 **Task**([works/task.md](../works/task.md)):写入(出生即 done)、rebuild(真后台任务)、`as_task` 查询、HTTP 超时升级的慢查询。一个 id 体系(`tk_<ds>_<hex>`,ds 自定位),一个 status/wait 面。

## `Task` 字段

```python
Task(
    id="tk_20260721_ab12…",
    op="insert" | "delete" | "rebuild" | "query",
    state="pending" | "running" | "done" | "failed" | "cancelled",
    error=None,            # failed:错误信息
    matched=None,          # delete:软删命中数
    stats=None,            # rebuild:{tables, rows, tombstones}
    query=None,            # op=query:SPL 文本(表只记 query,结果在文件)
    rows=None,             # op=query done:结果行数
    submitted_at=…, finished_at=…,
)
```

`Ticket` 是 `Task` 的旧名别名(`from seekbase import Ticket` 仍可用)。

## 方法

```python
await db.task_status(task_id) -> Task          # 单个状态(未知 id → NotFound)
await db.wait(task_id, poll=0.05) -> Task      # 轮询到 done/failed/cancelled
await db.tasks(limit=50) -> list[Task]         # 最近的 task,新在前(接口可查;不进 SQL 表;HTTP 形态暂固定 50)
await db.task_result(task_id) -> list[Row]     # 后台查询的结果行(从结果文件读回)
await db.cancel_task(task_id) -> Task          # 取消(见下:诚实边界)
```

### `task_result` 的状态规则

| task 状态 | 行为 |
|---|---|
| `done`(op=query) | 返回行 |
| `pending` / `running` | `QueryError`(还没完) |
| `failed` | `QueryError`(带原错误) |
| `cancelled` | `QueryError` |
| `done` 但结果文件已过保留期 GC | `QueryError`(结果过期) |
| op ≠ query | `QueryError`(写/rebuild 没有结果行) |

### `cancel_task` 的诚实边界

取消保证的是「**结果不再交付、状态可见**」:记录转 `cancelled`、结果丢弃。**已在读线程上执行中的 duck 段不会被中断**(它跑完后被丢弃);`db.close()` 时会对全部 cursor 发 `interrupt()` 兜底,跑飞的查询不会挂住关库。

## `db.rebuild` — 第一个真后台 task

```python
task_id = await db.rebuild() -> str    # 立即返回(pending),重放在后台跑
st = await db.wait(task_id)            # done → st.stats = {tables, rows, tombstones}
```

清空派生层、从 canonical 文件镜像整体重放(重新 embed、重建检索索引)。失败记 `failed + error`,不再是无声异常。

## 后台 task 的硬边界

- **max runtime 300s**:超时转 `failed("exceeded max task runtime")`——升级成 task 的查询没人等,必须有人杀。
- **结果落文件**:`<data_dir>/tasks/results/<id>.jsonl`;记录本身在按天分区的 `tasks/ds=*.jsonl`(状态变迁追加、末行为准)。
- **保留 GC**(open 时惰性清):日志 30 天、结果文件 7 天——结果比记录短命。

## HTTP 对应

`GET /v1/tasks`、`GET /v1/tasks/{id}`、`GET /v1/tasks/{id}/result`、`POST /v1/tasks/{id}/cancel`;`GET /v1/writes/{ticket}` 是 status 的兼容别名。慢查询的 `wait_ms` 升级见 [query.md](query.md#as_task)。
