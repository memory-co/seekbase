# tasks — 统一操作句柄

## 这个场景在测什么

一切「会完成的操作」共用一张 task 记录(docs/works/task.md):id 形如
`tk_<ds>_<hex>`(ds 自定位),按天分区 JSONL、状态变迁追加、末行为准。

1. **写 = 出生即 done**:insert 同步等完(read-your-write 不变),task 到手即
   done;`write_status` 旧名仍可用(ticket = 出生即 done 的 task)。
2. **rebuild = 真后台 task**:立即返回 pending,重放后台跑,`wait` 到 done + stats。
3. **as_task 查询**:后台执行,**记录只存 query 文本**,行落结果文件
   (`tasks/results/<id>.jsonl`),`task_result` 读回;失败记 `failed + error`。
4. **取消**:`cancel_task` → 记录转 cancelled、结果丢弃(诚实边界:执行中的
   duck 段不被中断;**close 时对全部 cursor `interrupt()` 兜底**,runaway 不挂关库)。
5. **tasks 列表**:接口可查(不进 SQL 表),新旧混排、末态呈现。
6. **HTTP 分层**:快路(`wait_ms` 内跑完)200 直回行、**零 task 开销**;超时
   → 查询继续跑、当场收编成 task、202 `{task, state}`,客户端转轮询;
   `as_task: true` 立即 202。嵌入形态不自动升级(await 是要答案)。

## 不在这测什么

- 写路径本身走 [`read_write/`](../read_write/);流句柄不是 task(无终态)。

## fixture 来源

- `db` / `pair`(conftest)+ 直接 ASGI HTTP 调用(验证 202/状态码)
