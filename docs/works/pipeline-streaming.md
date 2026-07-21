# pipeline-streaming — 把 bash 管道当简易流框架

> 状态:**设计稿(pipeline 方向,未落)**。这是 [operator-plugin.md §3.3](operator-plugin.md) 延后的「无界流 / `tail -f`」专文。一句话:**我们不造流引擎——bash 管道本身就是一个够用的穷人流框架**。一条 streaming 就是**一根源头无界、常驻不退的 bash 管道**;它的活只有「持续把新数据 ingest 进 DuckDB」,真正的查询/聚合全是 landed 表上的**有界 SQL**(走正常读路径)。**stream 写、query 读,两条路干净分开。**
>
> 依赖:[pipeline-runtime-optimize.md](pipeline-runtime-optimize.md)(runtime = duck / bash,有界性)、[operator-plugin.md](operator-plugin.md)(四方法、`run_bash`、`ctx.spawn`)、[concurrency.md](concurrency.md)(WriteService worker / 批处理 / ticket)、[store.md](store.md)(files-first)。

## 1. 定位:不造流引擎,借内核管道

真正的流处理器(Flink / Beam)自带一整套:背压、无界流、状态、窗口、水位线、精确一次、分布式扩缩。**seekbase 一样都不自己造**——因为 UNIX 管道**白送**了其中够用的几样:

| 要的 | 内核管道白送的 |
|---|---|
| 无界流 | `tail -F file`、`nc`、SSE、消费 MQ —— 都是**不会结束的 bash 进程** |
| 背压 | 管道满 → `write()` 阻塞 → 上游自然慢下来,内核管的 |
| 逐条/微批处理 | 进程按行读 stdin、写 stdout |
| 重启 | 进程挂了就重拉,从 checkpoint(文件 offset)续 |

**换来的边界**(§10 详述):**没有** exactly-once、事件时间窗口、水位线、跨流 join、分布式扩缩。要这些,要么把数据落进 DuckDB 后用有界 SQL 算,要么上真流处理器。这和整个项目「不重造一个更弱的 Flink」是同一条戒律。

> **分工钉死**:streaming 层只干**摄取 + 轻量逐条变换**;**一切分析都是 landed 表上的有界 SQL**(正常读路径)。想「实时统计过去 1 小时」?不在流里开窗——流只管把行 append 进去,你查的时候 `SELECT … WHERE ts > now()-1h` 一条有界 SQL 搞定(lambda 架构那味,但没有两套代码:摄取用 bash 流、查询用 DuckDB)。

## 2. 为什么 streaming source 必然 bash-常驻(是推论,不是选择)

这不是设计拍板,是[有界性规则](operator-plugin.md)推出来的:

```
无界 source(tail -F …)  ──①──►  没法当一张有限 SQL 关系
                        ──②──►  所以它没有 optimize_duck(表达不了)
                        ──③──►  runtime 指派只能把它钉在 bash
                        ──④──►  整条管道落 bash、常驻不退
```

- **① 无界 ⇒ 进不了 duck 的 `FROM`**:DuckDB 的 `WITH … FROM _in` 要一个**有限**关系;`tail -F` 永不结束,塞进去就是永久挂死(operator-plugin §3.3 那条编译期规则挡的正是它)。
- **② 所以一个能当 streaming source 的算子,本质上就没有 `optimize_duck`**——不是作者偷懒,是**「无界」和「一段有界 SQL」在语义上互斥**。它只有 `optimize_bash`(`tail -F` 这类原生命令)或 `run_bash`。
- **③④ 于是整条管道被钉在 bash runtime**(runtime 指派没得选,pipeline-runtime-optimize §4),编译成**一条常驻的 bash pipeline**——不像一次性 query 那样跑完就退,它**长活**,跟着源头一直流。

**这就是你要的那句话的完整推导**:能做 streaming 的 source ⟺ 无界 ⟺ 无 `optimize_duck` ⟺ bash 启动 + 常驻。

## 3. 关键区分:bash 遇见 duck 的两种方式

无界流最后总要「落进 DuckDB」,但**它不是作为关系流进去的**——那会触发 §2① 的挂死。有两条本质不同的路:

| | (a) 关系流入 `WITH` | (b) sink 命令式写入 |
|---|---|---|
| 形态 | duck 段 `FROM _in` 吃上游关系 | bash sink 进程按微批 `INSERT` |
| 有界要求 | **要有限**——无界 ⇒ 编译期报错 | **不要**——逐批写,天然配无界 |
| 无界能不能用 | ❌ | ✅ **这就是流落库的唯一合法路** |

> **无界流「落」进 DuckDB,靠的是 sink 一批批写、不是靠关系流入。** sink 是一个 `run_bash` 算子:读 stdin 的流、攒微批、`INSERT` 进库。它是 bash runtime 里的一个进程,**命令式地戳 DuckDB**,DuckDB 全程只当「被写的库」,从不当「吃无界关系的 `FROM`」。

## 4. 走一遍:监听 Claude Code 的 jsonl → 落库 → 可查

Claude Code 把每次会话的事件**一行一个 JSON** 追加进 `~/.claude/projects/<proj>/<session>.jsonl`。要把它变成可搜的活索引:

```
watch  ~/.claude/projects/**/*.jsonl          │ source:无界,tail -F 跟文件尾(bounded=False,只有 bash 形态)
  | jq -c '{session, ts, role, text:.message.text}' │ 中段:逐行抽字段(bash-native,流式)
  | ingest messages                            │ sink:run_bash,常驻写连接,微批 INSERT 进 DuckDB
```

- **`watch`**(source):对每个匹配文件 `tail -F -n +1`,新行一出现就吐到 stdout。**无界** ⇒ 整条钉在 bash、常驻。
- **`jq`**(中段):逐行 JSON 抽成 `{session, ts, role, text}`,内核管道流式接力、背压白送。
- **`ingest messages`**(sink):读 stdin 流,攒够一微批 → 经 WriteService 写进 `messages` 表(§5)。**无下游** ⇒ 推导为 sink。

落库之后,**查询是完全独立的一次性有界 query**,走正常读路径:

```
search messages "为什么 build 挂了"            ← 语义检索,有界 query,和上面那条流毫无关系
  | SELECT session, ts, text FROM _in WHERE role='assistant' ORDER BY ts DESC LIMIT 20
```

流在后台一直把新消息 append 进 `messages`;你随时发一条有界 query 查它。**摄取常驻、查询即席,互不阻塞**(读写分离,concurrency §3)。

## 5. 三类算子:source(无界)/ 中段 / sink(写库)

```python
class Watch(Source):                          # 无界 source
    name = "watch"
    bounded = False                           # ★ 唯一要声明的:我不会结束(operator-plugin §3.3)
    caps = {Cap.FS_READ}
    # 只有 bash 形态,没有 optimize_duck(无界表达不了,§2)
    def optimize_bash(self, args):
        return ["tail", "-F", "-n", "+1", args.glob]

class Ingest(Operator):                        # sink:写进 DuckDB,服务型(常驻写连接)
    name = "ingest"
    caps = {Cap.FS_WRITE}
    async def start(self, ctx):
        self.writer = await ctx.open_write_service(args.table)   # ★ 常驻,复用写路径
    def run_bash(self, stdin, stdout, args, ctx):                # 读流、攒微批、提交
        for batch in micro_batches(stdin, n=args.batch, ms=args.flush_ms):
            self.writer.submit(batch)                            #   经 WriteService(files-first + 索引 + 去重)
            checkpoint(args.glob, batch.last_offset)             # ★ 落 offset 在批**落库之后**(§7)
    async def stop(self):
        await self.writer.close()
```

- **source `bounded=False`** 是 operator-plugin §3.3 说的「唯一要作者声明的东西」——它一处声明,整条管道的 bash-常驻性质就被推导出来。
- **sink 是服务型**(`start`/`stop`):它持一个**常驻写连接**,不是每批重开(和 `search` 常驻引擎同理,operator-plugin §3.1)。
- 中段随便用 `jq`/`grep`/`sed` 或自写 `run_bash` 算子,只要是 bash-native 的流式段。

## 6. 常驻与生命周期:和一次性 query 的对照

| | 一次性 query | streaming |
|---|---|---|
| 源头 | 有界(search top-k / scan 快照) | **无界**(watch / follow) |
| 落地 runtime | duck 为主(能进 `WITH`) | **只能 bash,常驻** |
| 生命 | 跑完即退 | **长活,直到你停它** |
| 启动 | `db.query("…")` | `db.stream("…")` → 返回一个可 `stop()` 的 **StreamHandle** |
| 失败 | 整条重跑(读无副作用) | **从 checkpoint 续**(§7) |

`db.stream(pipeline)` 编译出那条常驻 bash pipeline、拉起、返回句柄;`handle.stop()` 发信号让进程链优雅收尾(sink flush 完最后一微批再退)。一个 seekbase 实例可以挂多条流(每条一个常驻进程链)。

## 7. 交付语义:at-least-once + 幂等 sink

bash 给的是**至少一次**,不是精确一次。靠两件事兜住:

- **checkpoint = 文件 offset**:sink 每提交完一微批,把「读到哪了」记下(小状态文件或一张 DuckDB 表)。重启时 source 从 offset 续读。**关键顺序**:offset 必须在**微批确实落库之后**才提交——否则「offset 记了、数据没落」那一瞬崩溃就丢数据。宁可反过来:崩在「落库了、offset 没记」→ 重启重放这一批 → 交给幂等 sink 去重。
- **幂等 sink**:seekbase 主键**写一次**(重复报错,time_machine.md)。所以流摄取的 `INSERT` 要走 **upsert / on-conflict-do-nothing**(按 pk 去重),否则重放会撞主键。**at-least-once + 幂等 = 事实上恰好一次**,不用真做两阶段提交。

> 这条链的持久性、原子性全**白嫖写路径**:sink 经 WriteService → files-first(store §3)→ 单写者串行 + FTS 批处理摊销(concurrency §6)。**streaming 没有自己的写机制,它就是写路径被一个无界源连续驱动。**

## 8. 窗口 / 聚合:别在流里做

想要「实时 top-N」「滑动窗口计数」?**不在 bash 流里开窗**(那就得自造水位线、状态、迟到处理——正是我们拒绝的 Flink 那套)。两条正路:

- **落原始 + 有界 SQL(默认)**:流只 append 原始行,你查的时候用一条有界 SQL 开窗:`SELECT count(*) FROM messages WHERE ts > now() - INTERVAL 1 HOUR`。DuckDB 的窗口函数、聚合、`QUALIFY` 全在这条有界 query 里,**一行没白写**。
- **要 live 物化 rollup**:再挂**第二条流**,micro-batch 把增量聚进一张 rollup 表(`INSERT … ON CONFLICT … DO UPDATE SET cnt = cnt + …`)。仍是「sink 命令式写 duck」,不是流内开窗。

**为什么这样够**:seekbase 的查询本来就快(DuckDB + 索引),「实时」在多数场景 = 「数据到得快 + 查得快」,而不是「流内预聚合」。摄取延迟由微批 flush 间隔(几十 ms~秒)决定,查询延迟由 DuckDB 决定,两个都够低。

## 9. 与写路径 / 时光机 / 检索的接缝

- **WriteService**(concurrency.md):sink 是 WriteService 的**连续生产者**;单写者 worker 天然串行化流入的微批,**批处理(§6)正好摊薄 FTS 重建**——流摄取尤其吃这个红利(不然每行一次重建直接崩)。
- **时光机**(time_machine.md):流进来的行照常带 `ds`/`deleted_ds`,as-of 查询、软删照常;**你能倒带到「昨天下午这条流写进来的样子」**。
- **检索**(search.md):`messages` 若声明 `searchable`,摄取时要 embed——**这是流摄取最贵的一步**(每行一次 embedder 网络调用)。对策:微批批量 embed(一次调用多条)、或对高频低价值流关掉 `searchable` 只留结构化。诚实标出来(§10)。
- **files-first**(store.md):sink 经写路径 ⇒ canonical 文件仍最先落地,流数据一样可从 files 重建;流崩了不致命。

## 10. 诚实的代价 / 边界

- **at-least-once,不是 exactly-once**:重启会重放最后一微批,靠幂等 sink 去重(§7)。要严格精确一次 = 两阶段提交,不做。
- **没有事件时间 / 水位线 / 窗口 / 迟到处理**:流层只摄取,开窗交给有界 SQL(§8)。乱序、迟到数据在 seekbase 里就是「按到达顺序 append 的行」,`ts` 只是一列普通数据,不是水位线(和 `ds ≠ watermark` 同一条,operator-plugin §3.3)。
- **embed on ingest 贵**:`searchable` 流每行一次向量化,是吞吐瓶颈;批量 embed 缓解,但仍是流摄取的主要成本。
- **每条流一个常驻进程链**:占 fd / 内存 / 一个 DuckDB 写连接。挂几十条流要认这笔常驻账(和 LanceDB fd 账同类,search §5)。
- **单文件有序,多文件无全局序**:`watch **/*.jsonl` 把多个文件的行交错进来,**没有跨文件全局顺序**——因为我们没有水位线去对齐。同一文件内有序(append-only)。跨源排序请落库后按 `ts` 有界 SQL 排。
- **不是 Flink**:重状态、跨流 join、窗口聚合、分布式扩缩——**这些该上真流处理器**。seekbase 的流是「把一个无界源持续灌进一个能查的库」,不是通用流计算。够用的场景:日志/事件/transcript 摄取、CDC 落库、把外部 feed 变成可搜表。
- **背压会顶到源头**:sink 慢(embed 慢 / 写慢)→ 管道满 → `tail` 阻塞 → 若源是**不可回放的实时流**(socket/SSE),阻塞期间的数据可能被上游丢。文件源(`tail -F`)无此问题(文件是持久缓冲,慢了就晚点读)。**优先文件源**。

## 11. 与其他文档

- [operator-plugin.md](operator-plugin.md):`run_bash` / `ctx.spawn` / `bounded=False` 的契约;本文是它 §3.3「无界流」那条延后指针的落地。
- [pipeline-runtime-optimize.md](pipeline-runtime-optimize.md):为什么无界 ⇒ 只能 bash(§2 的推导来自它的有界性规则 + runtime 指派)。
- [concurrency.md](concurrency.md):sink 经 WriteService 落库——单写者串行 + 批处理摊销 FTS,流摄取白嫖这套。
- [store.md](store.md):files-first;流数据一样从 canonical 文件可重建。
- [time_machine.md](time_machine.md):流进来的行照常 `ds`/`deleted_ds`,可倒带。
- [search.md](search.md):`searchable` 流摄取的 embed 成本与批量对策。
