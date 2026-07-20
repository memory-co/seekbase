# concurrency — async 执行、读写分离、写管道(设计)

> 状态:**已落**——Bridge(M1)、a 模式写 worker + 批处理(§4/§5/§6,`service/write_service.py`)、读写分离(§3,`runtime/readpool.py`:读走 cursor 池,MVCC 并发,不排在写后)。本文讲 seekbase 怎么在 async 世界里安全跑同步的 DuckDB:为什么有 Bridge、为什么读曾排在写后面、怎么把读拆出去、以及把写收敛成一条**看得见生命周期**的 worker(队列 + ticket)。

## 1. 起点:async 门面 ↔ 同步阻塞的 DuckDB

seekbase 对外是 `async`(`await db.query(...)`),跑在**事件循环**(单线程)上;DuckDB 的 `conn.execute()` 是**同步阻塞**的。直接在循环里调它,一条大查询就把**所有**协程冻住。所以任何 async 程序调阻塞库,都必须把阻塞调用甩到别的线程(`loop.run_in_executor`)。

**`Bridge`**(`runtime/bridge.py`)就是这层:`ThreadPoolExecutor(max_workers=1)` + `run_in_executor`。`max_workers=1` 让所有 DuckDB 操作在**同一条线程串行**(满足连接线程亲和 + 单写者)。`await bridge.run(fn)` = 把阻塞的 `fn` 丢到那条线程、循环不被卡、干完唤醒协程。

## 2. 现状的问题:一条线程把「读」也串行了

今天 Bridge 串行化**一切** DuckDB 访问——`query` 也走 `bridge.run`。于是**读排在写后面**:一次**检索管道**会卡在前面的写、尤其是 **FTS 重建**(duck-vss 后端每次 insert 同步重建,`create_fts_index` 是 O(表大小),大表几百毫秒~秒级;lance 后端无此步)之后。对读多写少的 memory 场景,这是主要的并发痛点。

## 3. 读写分离:读走 `.cursor()` + MVCC(不是 read-only 连接)

DuckDB 单进程里**开不了「读连接用 read_only、写连接不用」**——实测:同一文件用不同配置再 `connect(read_only=True)` 直接报 `Can't open a connection to same database file with a different configuration`。可行的是:

- **一个 DuckDB 实例**(读写打开一次);
- **读走 `conn.cursor()`** 拿共享同实例的额外连接,靠 **MVCC 并发读**——读方看一致快照,**不被进行中的写 / FTS 重建阻塞**(§[search.md](search.md) 里验证过:重建期另一条连接照常读到旧索引、不挂不空)。
- **read-only 由语句层守卫**(现有的 single-`SELECT` 判定)保证,不靠连接 flag;
- **读有自己的执行线程**(独立于写 worker 的小线程池 + cursor):读仍是阻塞调用,也要 `run_in_executor`,但走**另一个 executor**,所以不排在写后面。

净效果:**写单线程串行(单写者),读并发(MVCC),互不排队**。

> **已落**(`runtime/readpool.py` `ReadPool`):开库时在 bridge 线程上从主连接建 N 个 cursor,读走一个小线程池、每读借一个 cursor;`StoreService.run_query` 从写 bridge 挪到 ReadPool。实测:写进行中并发读 ~12ms 不被挡;20 写 + 20 读交错无异常、快照一致。

## 4. `WriteService` 里的写 worker:一张 ticket 从 pending 到 done

写侧不另造概念:**一次写就是一张 ticket**,`WriteService` 里一个显式的 worker 把它从 pending 驱动到 done。worker 是一段能读的循环,一次写的一生(入队 → 取出 → 执行 → 发 ticket → 唤醒)在源码里摊开,不埋在 executor 黑盒里:

```python
class WriteService:                       # 拥有:写连接 + worker + ticket 日志
    async def start(self):
        self._worker = asyncio.create_task(self._worker_loop())     # ★ worker 起来,看得见

    async def submit(self, op) -> Ticket:                   # 提交一次写
        fut = asyncio.get_running_loop().create_future()
        await self._q.put((op, fut))
        return await fut                                    # 模式 a:调用方等完成

    async def _worker_loop(self):          # ★★ 写 worker 的一生
        while not self._stop:
            op, fut = await self._q.get()                   # 多→一:所有写在这排队(串行/单写者)
            try:
                ticket = await self._execute(op)            # 校验→embed→files→db(阻塞部分经 Bridge)
                fut.set_result(ticket)                      # 完成信号(同一循环 → 线程安全)
            except Exception as e:
                fut.set_exception(e)
            finally:
                self._q.task_done()
```

- **Bridge 不消失,沉到底层**:`_execute` 里真正阻塞的 DuckDB / 文件调用仍走 `bridge.run(...)`(不卡循环)。**Bridge = 「把阻塞活甩到线程」的底层原语;`WriteService` 的 worker = 你能看到的写生命周期**——一下一上,各司其职。
- **执行 + ticket 在一处**:队列的「多→一」是串行/单写者,循环末尾发 ticket 是完成标记——**一次写的执行与记录都在 `WriteService`**。没有独立的 `TicketService`、也没有 `WritePipeline`;**ticket 只是 `WriteService` 内的概念**,不是并列组件(见 [ticket.md](ticket.md))。
- **asyncio 原语(`Queue`/`Future`)只在事件循环线程内碰才安全**。本设计的 worker 是**协程**(`create_task`,**不是线程**),所以 `submit`(生产)和 `_worker_loop`(消费)都在循环线程上——**队列 / Future 从不跨线程**。唯一过线程边界的,是 `_execute` 里那句 `bridge.run(...)`(= `run_in_executor`,被祝福的过线程桥,内部 `wrap_future` 做线程安全完成通知);那条 DuckDB 线程**永远碰不到队列 / Future**。
  > 反例:若把 worker 做成**真线程**,再从里面用 `asyncio.Queue` / 裸 `asyncio.Event.set()` 就是竞态 bug——得换线程安全队列(如 `janus`)或 `loop.call_soon_threadsafe`。把 worker 做成协程、只下沉那一句阻塞调用,正是为了避开这个。

## 5. a 同步 / b 异步 = `submit` 边缘的一行策略

有了显式 worker,**a/b 只是「调用方等不等」**,worker 循环完全一样:

| | 调用方 | 语义 | 代价 |
|---|---|---|---|
| **a 同步** | `return await write.submit(op)` | 拿到即 `done`,**read-your-write 不变**(写完即可搜) | 调用方阻塞在 embed 网络 + 写 |
| **b 异步** | `t = write.enqueue(op); return t`(立刻回 `pending`,worker 事后改 `done`) | 写不阻塞、可 fire-and-forget | **重新引入最终一致窗口**(insert 返回后、worker 未处理前 `search` 段搜不到,需 `wait(ticket)`)——即把 **outbox** 请回来 |

- **b 不再踩 HNSW 段错误**(worker 做的是完整 inline-embed 写,不是「先写 NULL 再 UPDATE」那个崩法),技术可行;但它是路线之争(同步简单 vs 异步吞吐)。
- **已选 a 并落地**:`insert`/`delete` 经 `WriteService` 的 worker,批处理实测 **8 并发 insert → FTS 重建 1 次**、写完即可搜(read-your-write)。**b 保留为未来选项**:同一个 worker,`submit` 不 `await`(立刻回 pending ticket)即可切换。

## 6. 白赚:批处理(worker 的自然扩展)

worker 可以**一次从队列抓 N 个待写、攒成一个事务**:大赢不是省 commit,是 **FTS 重建从「每次 insert 一次」变成「每批一次」**(O(表大小) 的活摊销)。同步语义不破(每个调用方仍等自己那份完成)。这是显式 worker 相对 `bridge.run` 唯一「多买到」的东西,也是要不要建它的关键理由之一。

## 7. 目标结构

```
读路径  query = 管道执行(PipelineService)
  → transform 段的 DuckDB SQL 走读 executor(小线程池)+ conn.cursor()   并发读,MVCC,不排在写后
  → source 段(search)走 SearchService 后端;算子段起子进程(见 operator-registry.md)
写路径  insert / delete / rebuild
  → WriteService(写连接 + 单 worker 队列 + ticket 日志)
       worker: 队列多→一 → _execute(经 Bridge) → 发 ticket → 唤醒
Bridge  runtime/:把阻塞调用甩到线程的底层原语(读、写两侧都用)
```

- `WriteService`(`service/write_service.py`)一个组件吃下写的全部:校验/embed/顺序编排 + worker 串行执行 + 发/记 ticket;`Bridge` 留 `runtime/`(通用原语,读写两侧都用)。
- `rebuild`(`admin_service`)是最重的写,也经 `WriteService` 的 worker 走,产出一张 ticket。读侧无 ticket、无 worker,直接走读 cursor。

## 8. 诚实的代价

- 显式 worker 比 `bridge.run(fn)` 多写代码(队列 + 循环 + Future);**换来的正是「一条能读的写生命周期」**+ 批处理 / a/b 的地基。若永远同步、不批、不异步,它就是「Bridge 加可见步骤」——是否值,取决于你多看重可读性与后续扩展。
- 读写分离要多一个读 executor + cursor 管理;收益是读不被 FTS 重建阻塞(读多写少场景值)。
- 一致性:a 保 read-your-write;b 认最终一致(§5)。

## 9. 与其他文档

- [architecture.md](architecture.md):整体分层;本文细化其中的并发/执行基座。
- [ticket.md](ticket.md):ticket 是写的完成标记,由 WriteService 的 worker 末尾发出。
- [search.md](search.md):FTS 同步重建的成本(读写分离要缓解的正是它)。
- [store.md](store.md):写入流水(files-first → duck.db),worker `_execute` 跑的就是它。
