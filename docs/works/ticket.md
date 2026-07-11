# ticket — 写回执 / 操作日志(设计)

> 状态:**设计**(未落地)。当前实现是一个独立 `TicketService` 里的内存 dict;本文把它换成一个**独立、落盘、状态-only 的回执日志**,并把它**收进 `WriteService`**——**ticket 不是一个独立组件,而是 `WriteService` 内的一个概念**:一次写**就是**一张 ticket,由 `WriteService` 发出、驱动到 `done`、记录。没有 `WritePipeline`、也没有独立的 `TicketService`。对外用法见 [api/insert.md](../api/insert.md) 的 `ticket` 字段。

## 1. 定位:ticket 是同步写的「回执」,不是异步 job 句柄

seekbase 的写是**同步**的:`insert`/`delete`/`rebuild` 在一次调用里把数据落库(files + duck.db + FTS),返回时**已经 `done`**。ticket 是这次写的**操作级完成标记**,写在整条流水的**最后一步**——所以「ticket 存在且 `done`」= 那次写(可能很重、一次跨多个分区)**真的从头跑完了**。它有两个用途:

- **操作完成保证 / 审计**:重写入 / `rebuild` 跨所有 `ds` 分区这种活,底层是许多分区各自 append,**ticket 是它们之上唯一的「操作级 done」**——一行盖住整个操作,不管碰了几个数据分区;也是「哪次写发生、什么 op、影响几行、几点完成」的操作级审计。
- **两形态 API 对称**:写返回 `ticket`,`wait`/`write_status` 能查——嵌入与 HTTP 一套接口。

**和文件镜像是两层、不冲突**:镜像(jsonl,files-first + fsync)保**数据不丢**;ticket 保**这次操作干完了**。ticket 不是异步 pending 句柄(写同步、`state` 恒 `done`),也**不是数据层的原子提交闸**(那是 files-first 的活,见 §4)。`matched`(delete)/`stats`(rebuild)是随 op 变化的载荷。

## 2. 为什么现在的内存 dict 不行

`TicketService` 用 `self._tickets: dict[str, Ticket]`,`issue` 塞、`status` 查、从不删。三个问题:

1. **无界增长**:每次写攒一个 Ticket 进 dict,永不淘汰 → 慢性内存泄漏。
2. **不持久**:进程重启,dict 清空 → 重启前的 ticket 一律 `NotFound`(404)。
3. **进程本地**:不跨进程共享(具名 `:memory:` 也只同进程多连接共享,**不跨进程**——实测确认)。

## 3. 设计:独立、落盘、append-only、状态-only

**一个独立的按天分区 JSONL 日志**,复用文件镜像那套机制(append + fsync + `ds=` 分区 + 删旧分区即清理),**不引入第二个 DuckDB 引擎**:

```
<data_dir>/
  duck.db                 # 业务(结构化 + vss + fts)
  files/                  # 业务文件镜像(canonical)
  tickets/                # ★ 独立的回执日志
    ds=YYYYMMDD.jsonl      #   按天分区,每行一个 ticket 记录
```

- **独立于业务 duck.db**:`rebuild()` 只清/重灌 duck.db,**不碰 tickets/**;也就不违反「duck.db 全部由文件镜像派生」这条铁律。ticket 日志是**独立的操作日志**,既非业务 canonical、也非派生。
- **每行记什么(状态-only,不记正文)**:`{ticket, op, state, matched?, stats?, created_at}`。**永不写入写入正文**——数据本身在 `files/` + `duck.db` 里,这里只留回执。
- **自定位 id**:ticket id 里嵌入日期,如 `wr_<YYYYMMDD>_<hex>`。`status(id)` 从 id 解析出 `ds`,**直接打开那一天的分区**扫到该行——O(一天),不用维护会随保留期增长的内存索引。

## 4. 生命周期与一致性:ticket 是**最后一步**,不是提交闸

一次 `insert` 的顺序(§[store.md](store.md) §6.2):

```
① store.validate(校验 + dup-pk)   ② embedding.embed(内联)
③ files.write_puts   ← canonical 先落地(files-first)
④ store.commit_rows  ← duck.db INSERT + FTS(一个 bridge 块)
⑤ tickets.append     ← ★ 回执最后写,记 done
```

- **两种「提交」别混**:
  - **① 数据持久性**(数据丢不丢):由 `files-first`(每行 append + fsync)保证。ticket **不是**这个——tickets 和业务是两个独立库、**无跨库事务**,当不了数据层的原子提交闸(「ticket 写成功 = 数据提交」不成立,业务写成功 + ticket 写失败照样脑裂)。
  - **② 操作完成**(这次写整体跑完没):**正是 ticket 的活**。它写在流水**最后一步**,所以 **done-ticket ⟺ 它之前的 files + duck.db 全部完成**。重写入 / 跨多分区的 `rebuild` 崩在中途时:镜像 + `repair`(§[store.md](store.md))保证**数据不丢、从文件侧收敛**,而**有没有 done-ticket 告诉你这次操作到底干完没**——数据文件本身分不清「完整跑完」和「崩在第 5000 行」。
- **一个诚实的窗口**:ticket 独立库、末尾追加,所以「duck.db 已提交、ticket 还没落」那一瞬崩溃,会有「操作其实完成了但没 ticket」的**假阴性**。对判断重活干完没 / 遥测**够用**(repair 兜数据;最坏把幂等操作如 `rebuild` 重跑一遍)。要**严格 exactly-once** 的完成语义,才需要两阶段 ticket(begin→end)或把 ticket 放回同库同事务——过度,不做。
- **`status(id)` 语义**:从 id 定位分区扫描 → 命中回 `done`(+ matched/stats);未命中(未知 id 或已被清理的旧 ticket)→ `NotFound`(404)。`wait` 同步下立即返回(恒 done)。

## 5. 保留与清理

- **默认不清理**:状态-only 一行 ~100–200B。按 1 万写/天 ≈ 2MB/天;一年不清 ~700MB——对 memory 这种写量基本无感。
- **可选定期清理**:删掉旧的 `tickets/ds=…` 目录即可(和文件镜像删旧分区**一模一样**,零新机制)。默认关;想开就配一个保留期(如 30 天),超期的 ticket 查不到 → `NotFound`(可接受:没人会去查一个 30 天前、早就 done 的回执)。

## 6. 为什么是 JSONL 分区,而不是 DuckDB / 内存

| 方案 | 结论 |
|---|---|
| 内存 dict(现状) | 泄漏 + 重启即失 + 进程本地 —— 见 §2,弃 |
| 具名内存 DuckDB(`:memory:name`) | **不能跨进程**(实测);且完成即清 vs 可查询自相矛盾;倒置持久性 —— 弃 |
| 共享业务 duck.db 里加一张表 | 成为唯一「非派生」表,`rebuild` 会清空它;写路径耦合 —— 弃 |
| 独立 DuckDB 文件 | 可行,但为一个「id→状态」小映射多起一个引擎 + 连接 + fd;**只有当你想对 ticket 跑 SQL**(「今天几次 insert / 哪些 op 失败」)才值 |
| **独立 JSONL 按天分区(选)** | 复用现成 append+分区+删分区机制,不加引擎,和「文件是 canonical」调性一致;代价:按 id 查靠自定位 id 扫一天分区(同步写下 ticket 查询极罕见,够用) |

**取舍**:除非明确要 SQL 查询 ticket,否则 JSONL 分区更轻、更一致。要 SQL 就换独立 DuckDB 文件,§3/§4/§5 的其余设计不变。

> 还有一个更激进的选项(**A:干脆不存**):写方法直接返回自包含的完整 `Ticket`,`wait` 拿到即返回,删掉整个存储层。同步写下这最干净——但放弃了操作级审计与「未知 ticket→404」契约。本设计选择保留 ticket 日志,是为了那份**可持久、可审计的操作记录**。

## 7. 分层落地:ticket 住在 `WriteService` 里

一个概念(`Ticket`)+ 一个组件(`WriteService`),不再有独立的 ticket 组件:

- **`WriteService`**(`service/write_service.py`)**拥有 ticket 的整条生命**:一次写进来 → 发一张 `Ticket`(id 含 ds,§3)→ 驱动它跑完(校验→embed→files→db,阻塞部分经 Bridge)→ `done` 后 `to_wire()` **append 进 `tickets/ds=…jsonl`**(单写者串行,和文件镜像一致)→ 返回;`status(id)` → 从 id 定位分区扫描。**`issue` / `status` / 落盘都是 `WriteService` 的内部方法**(原 `TicketService` 并入)。
- **`Ticket`**(`struct/ticket.py`):数据对象不变(`id/op/state/matched/stats` + `to_wire`/`from_wire`);JSONL 一行就是 `to_wire()`。它是贯穿始终的那个概念——「一次写」。
- **谁调**:`insert`/`delete` 走 `WriteService`;`rebuild`(`admin_service`)也在写完后经 `WriteService` 发/记 ticket;`status` 端点(`api/writes.py`)/ `client.write_status` → `WriteService.status`。

## 8. 与其他文档

- [store.md](store.md):写入流水(files-first → duck.db),ticket 是其最后一步。
- [concurrency.md](concurrency.md):`WriteService` 的写 worker——一张 ticket 从 pending 到 done 怎么被驱动。
- [architecture.md](architecture.md):service 分层里 `WriteService` 的位置。
- [api/insert.md](../api/insert.md):`ticket` 字段的对外形态。
