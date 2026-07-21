# task — 统一的操作句柄:写回执 + 慢查询 + rebuild

> 状态:**已落**(`service/task_service.py` + `struct/task.py`;原 ticket.md 收编为本文 §2——**ticket = 出生即 done 的 task**,写侧语义一字未变)。一切「会完成的操作」共用一张 task 记录:**写入**(同步,出生即 done)、**rebuild**(真 pending→done 后台任务)、**慢查询**(显式 `as_task` / HTTP 超时升级)。一个 id 体系、一个 `status`/`wait` 面、一份按天分区的 JSONL 日志。
>
> 先例:BigQuery jobs(`jobs.query` 带 `timeoutMs`,超时回 job id 转轮询)、Snowflake async query。**「有界等待,超时升级成句柄」**是被验证过的形态。

## 1. 一条 task 记录

```
task = { id: tk_<ds>_<hex>,            ← ds 嵌在 id 里,status 直达分区(自定位,原 ticket 机制)
         op: insert|delete|rebuild|query,
         state: pending|running|done|failed|cancelled,
         query: "<SPL 文本>",          ← 只记 query 文本(op=query 时);结果不进表
         rows: <行数>,                 ← done 后:结果行数
         error, matched, stats,        ← failed 的错误 / delete 命中数 / rebuild 统计
         submitted_at, finished_at }
```

三条已拍板的边界:

- **结果持久化到文件,表只记 query**:结果行落 `data_dir/tasks/results/<id>.jsonl`,task 记录只存 query 文本 + 行数。结果文件按**保留期 GC**(默认 7 天,open 时清)。
- **tasks 接口可查即可**:`db.tasks()` / `db.task_status(id)` / `db.task_result(id)` / `db.cancel_task(id)`(HTTP:`GET /v1/tasks[/{id}[/result]]`、`POST /v1/tasks/{id}/cancel`)。**不**暴露成 SQL 系统表——要就查接口。
- **写入仍然等完(模式 a 不变)**:`insert` 必须等 task done 才返回——**否则查不到**(read-your-write 是刚性需求)。合并 ticket **只统一记录,不夹带异步写**;模式 b(异步写)是 task 表之上未来的显式 flag,这次不开。

## 2. 写回执 = 出生即 done 的 task(原 ticket.md)

写是同步的(concurrency.md §5 模式 a):一次写在 worker 里跑完 files-first + 落库 + 索引,**最后一步**才追加 task 记录——所以 **done-task ⟺ 整个写完成**,它是回执、不是提交闸:

- **两种「提交」别混**:数据持久性由 files-first 保证(task 和业务库无跨库事务,当不了原子闸);**操作完成**才是 task 的活——数据文件本身分不清「完整跑完」和「崩在半途」,task 记录补这个语义。
- **日志形态**:独立、落盘、状态-only 的按天分区 JSONL(`data_dir/tasks/ds=YYYYMMDD.jsonl`);**状态变迁 = 追加一行,末行为准**(event-sourcing 式,文件纯 append 哲学不破)。为什么不是 DuckDB 表:回执要在库损坏时仍可读(rebuild 期间也要能查 task)、要 grep-friendly、要和业务数据的生命周期解耦。
- **诚实窗口**:「库已提交、task 未落」那一瞬崩溃 → 操作完成但无记录(假阴性)。对判断「重活干完没」够用;严格 exactly-once 完成语义要两阶段,过度,不做。

## 3. rebuild:第一个真 pending→done 的 task

旧形态 rebuild 阻塞整个 `await` 到重放完才发 ticket——最重的写反而最没进度可言。现在 `db.rebuild()` **立即**返回 pending task,重放在后台跑,完成后记 `done + stats`(失败记 `failed + error`)。`await db.wait(task_id)` 语义照旧。

## 4. 慢查询:显式 as_task + HTTP 超时升级

**分层拍板(嵌入别默认升级)**:嵌入式里 `await` 60 秒对事件循环零成本,调用方在 await 就是要答案;HTTP 上长连接才是真实成本(代理超时、连接池)。所以:

| 形态 | 默认 | 显式 task |
|---|---|---|
| 嵌入 | 一直 await(不变,不产 task) | `db.query(..., as_task=True)` → 立即回 task id |
| HTTP | `wait_ms=5000` 内跑完 → 200 rows(**零 task 开销**) | 请求带 `as_task: true` → 202 task |
| HTTP 超时 | 查询**继续跑**,当场**收编**成 task → 202 `{task, state:running}`,客户端转轮询 | — |

- **快路零开销**:HTTP 常规查询不产 task 记录、不写结果文件;只有超时**收编**(adopt)或显式 as_task 才落记录——不为可观测性向常规路径收税。
- **升级 ≠ 取消**:超时升级后查询继续在 ReadPool 线程上跑;完成时结果落文件、task 记 done。
- 完成后:`db.task_result(id)` 读结果文件回行;pending/running 报「未完成」;failed 抛原错误。

## 5. 取消与超时(诚实边界)

- `cancel_task(id)`:取消 asyncio 包装、task 记 `cancelled`、结果丢弃。**已在 ReadPool 线程上执行中的 duck 段不会被中断**(要接 `conn.interrupt()` 得跟踪「哪条查询占着哪个 cursor」,后续再做)——cancel 保证的是「结果不再交付、状态可见」,不是「算力立刻归还」。
- **max runtime**(`task_timeout`,默认 300s):后台 task 超时转 `failed("timeout")`。没有它,升级成 task 的 runaway 查询**没人等、没人杀**——比同步形态更危险,所以这条是硬配套。
- task 化**不释放 cursor**:查询还在跑就还占着 ReadPool 的一个 slot。它解决「调用方/连接等」,不解决 cursor 饥饿(那是独立的池隔离问题)。

## 6. 保留与清理

- task 日志:按天分区,过保留期(默认 30 天)整分区删。
- 结果文件:`results/<id>.jsonl`,过保留期(默认 7 天)删——**结果比记录短命**(记录是审计,结果是缓存)。
- 两者都在 open 时惰性清,无后台线程。

## 7. 不并入的:stream

`db.stream` 的句柄**不是** task:流是无终态的常驻体,`stop()` 语义和「完成」不同。留在 StreamHandle;真要统一等出现「有限流任务」再说。

## 8. 与其他文档

- [concurrency.md](concurrency.md):模式 a 写 worker;task 记录在 worker 末尾追加(原 ticket 位置);模式 b 若开,就是「submit 不 await + task 出生 pending」——task 表正是它缺的基础设施。
- [pipeline-as-anything.md](pipeline-as-anything.md):query 的编译执行不变;task 只是把「等结果」这层从调用方剥出来。
- [store.md](store.md):files-first 保数据;task 保「操作完成」语义。
- [api/insert.md](../api/insert.md):对外的 task 字段。
