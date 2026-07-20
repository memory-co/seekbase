# tool-plugin — 写一个工具:可插拔**算子**的契约与实现

> 状态:**设计稿(pipeline 方向,未落)**。[tool-registry.md](tool-registry.md) 讲**系统视角**(registry 怎么存、权限怎么判);本文讲**作者视角**:你要给 seekbase 加一个管道工具(`search` 是内建的一个、`grep`/`find`/`sh` 是另几个),得实现一个什么样的 **plugin**?
>
> **一个 plugin = 一个算子(operator)。** 管道就是一串算子,框架只定**算子 ABI**,谁都能往里插——`search`、`grep`、`jq`、以及 SQL 段,在 ABI 面前是同一种东西。这套算子契约**参考 Flink**:一次性的 `run` 之外还有推式的 `process(chunk, ctx, out)`(§3.2),以及**有界性(boundedness)**——数据流会不会结束(§3.3)。**但只借这两样**,watermark / checkpoint / keyed state / 分布式那一整套不借(§3.3 末的表)。
>
> 依赖:[tool-registry.md](tool-registry.md)(Tool 记录、caps 分级、能力×策略权限)、[pipeline-as-anything.md](pipeline-as-anything.md)(`_in` 表 ABI、source/transform/tool/sink 类别、SQL 是缺省)。

## 1. 定位:一个 tool = 一个 plugin = 一个算子

管道里每段非-SQL 的 verb 都由一个 plugin 支撑。**框架不认识 `search` 也不认识 `grep`,它只认识「算子」**——一个能吃数据、吐数据的东西;`search` 和你自己写的工具插进的是同一个 ABI(这就是「可插拔算子机制」的全部意思)。一个 plugin 就是一条**注册记录**:

```python
Tool(
    name    = "grep",
    accepts = {Fmt.TABLE},                  # 能收哪些输入格式(source 用 {Fmt.NONE},§8)
    emits   = Fmt.TABLE,                    # 吐哪种输出格式
    args    = "<pattern> [--field <col>]",  # 供解析 + --help 的签名
    caps    = {Cap.PURE},                   # 诚实声明碰什么外界资源(§6)
    out     = grep_out_schema,              # emits=TABLE 时:输出表的列 schema(§7)
    run     = grep_run,                     # ★ (in_data, args, ctx) -> out_data
)
```

作者要填的就这几格,重点是 **`accepts`/`emits`**(格式契约,§8)、**`run`**(§3)、**`caps`**(§6)、**`out`**(§7);另有两档**按需才填**的:**服务型工具**(背后有常驻进程,如 `search`)加 **`start`/`stop`** 生命周期(§3.1);**流式工具**(要内存上界或早停)把 `run` 换成 **`process`** 并声明 **`bounded`**(§3.2/§3.3)。`grep` 这类最简工具两档都不用填。

> **不用 `kind`**:plugin 不声明自己是 source/tool/sink——它只声明**收什么格式、吐什么格式**,是不是 source(无输入)、sink(终端输出)由格式**推导**(§8)。分类是格式的投影,不是另一个要维护的字段。

## 2. 三种作者形态(按复杂度挑)

| 形态 | 怎么写 | 适合 | 序列化 |
|---|---|---|---|
| **函数式(最小)** | 一个 `Tool(..., run=fn)`,`fn` 收/返 Arrow 表 | PURE / 轻量、进程内(grep、sed-over-table) | 无(进程内零拷) |
| **类式(Protocol)** | 实现 `ToolPlugin` 协议(`name/accepts/emits/caps/out_schema/run` + 可选 `start/stop`) | 要参数校验、动态 schema、或**带常驻服务/进程的工具**(如 `search`,§3.1) | 无 |
| **外部命令式** | `ExternalTool(name, argv, caps, accepts=…, emits=…)` | 包一个现成 CLI(`jq`、任意脚本) | 框架管(按格式 coercion,§4) |

- **外部命令式作者不碰序列化**:你只给 `argv` 模板 + 编码格式,框架把 `_in` 序列化进 stdin、把 stdout 解析回表(见 [pipeline-as-anything.md §4](pipeline-as-anything.md))。这是把任意 CLI 变工具的最省路径,代价是它天然带 `EXEC` 能力、默认受最严策略约束(§6)。
- **函数式/类式是进程内的**:收/返的是 Arrow-backed 关系,和下游 DuckSQL 段零拷交换。

## 3. 核心契约:`run(in_data, args, ctx) → out_data`

所有形态最终归到这一个签名(`in_data`/`out_data` 的类型随你声明的 `accepts`/`emits` 变——`TABLE` 是关系,`JSONL`/`BYTES` 是字节流):

```python
def run(in_data: In | None, args: Args, ctx: ToolCtx) -> Out:
    ...                                    # In/Out 由 accepts/emits 决定(§4/§8);source 的 in_data=None
```

- **`in_data`**:上一段的产物 `_in`,**按你声明的 `accepts` 格式**递进来(§4/§8)。`accepts={NONE}`(source)时为 `None`(无输入、自己产数据);`accepts={TABLE}` 时是一张 Arrow-backed 关系,`accepts={JSONL}` 时是 JSONL 字节流,等等。**只读**——不要原地改它,产一份新数据返回。
- **`args`**:按 `Tool.args` 签名解析好的参数(位置 + 选项);解析失败在编译期就报,`run` 里拿到的一定合法。
- **`ctx`**:注入的执行上下文(§5)——**你碰外界的唯一门**(读文件、联网、起子进程都得过 `ctx`,不能用 ambient authority)。
- **返回**:一张 **schema 稳定**的表(§7),恒被绑成新的 `_in` 交给下一段。

> 一段的执行 = `_in = tool.run(_in, args, ctx)`。整条管道就是拿这个 `run` 不断折叠 `_in`(pipeline §6)。

### 3.1 无状态 vs 服务型:要不要常驻进程

`run` 的签名一样,但工具按**背后有没有常驻状态**分两类——**这正是 `grep` 简单、`search` 复杂的根源**:

- **无状态工具(如 `grep`)**:**没有常驻进程**,`run` 是纯函数——每次调用自给自足(拿到 `_in` 就地过滤),调用完什么都不留。**没有 `start`/`stop`**,注册一个 `run` 就够。
- **服务型工具(如 `search`)**:背后是一个**常驻服务/进程**——向量引擎(LanceDB / DuckDB-vss 的连接)、**加载进 RAM 的 HNSW 索引**、embedder 客户端/连接池。这些**开一次、复用多次**,**绝不能每次 `run` 都重开**(开引擎、把索引载进内存是重活)。所以服务型 plugin 多实现两个生命周期钩子:

  | 钩子 | 何时 | 干什么 |
  |---|---|---|
  | `start(ctx) → handle` | `open` 时一次 | 拉起常驻服务:开引擎连接、把索引载进 RAM、暖 embedder;把长活 handle 存成字段 |
  | `run(in_data, args, ctx)` | 每次调用 | **复用** handle(不重开),做一次检索/变换 |
  | `stop(handle)` | `close` 时一次 | 拆常驻服务:关连接、释放索引内存 |

  用**类式** plugin 承载最自然:实例在 `start` 里把 handle 存字段、`run` 里复用、`stop` 里拆。无状态工具则这两个钩子都不实现——框架看到没有 `start` 就当它零常驻。

> 分界线:**「每次调用要不要复用一份贵的、开一次的资源」**。要 → 服务型(start/run/stop);不要 → 无状态(只 run)。`search` 的引擎 + RAM 常驻索引就是那份贵资源,`grep` 什么都不用留。

### 3.2 一次性 `run` vs 流式 `process`:数据怎么流(参考 Flink 的算子)

`run(in_data) → out_data` 隐含一条强假设:**上游会结束,而且结束时的全量能装进内存**。三个具体症状:

| 症状 | 例子 | 后果 |
|---|---|---|
| 段边界 = 全量物化 | `scan cards \| jq …` | 整张表先序列化才喂进子进程,表多大内存多高 |
| 下游 `LIMIT` 不能早停 | `scan cards \| grep 'ERROR' \| SELECT * FROM _in LIMIT 20` | `grep` 过完全表才交出去,尽管只要 20 行 |
| 上游不结束 = 永久挂起 | `sh 'tail -f app.log' \| SELECT …` | `run` 在等一个永远不来的「输入结束」——**不是报错,是挂死** |

所以算子契约有**第二种形态**,形状照抄 Flink 的 `ProcessFunction`——框架把数据**推**给你,你往 `Collector` 里吐:

```python
class GrepStream:                                  # 流式算子:实现 process,而不是 run
    name, accepts, emits = "grep", {Fmt.TABLE}, Fmt.TABLE
    bounded = "inherit"                            # 有界性透传上游(§3.3)

    def process(self, chunk, args, ctx, out):      # ★ 上游每来一批就调一次
        out.emit(filter_batch(chunk, args))        #   吐 0..N 批;吐多少、何时吐由你定

    def on_end(self, args, ctx, out):              # ★ 上游结束(仅有界流会到达)
        pass                                       #   收尾:flush 攒着的东西
```

- **粒度是 Arrow RecordBatch,不是行**。Flink 敢做行粒度是因为 JVM + 算子链内联;Python 里每行一次回调直接判死。**一批 ~2k 行**,是「流式的好处」与「解释器开销」之间的实际平衡点。
- **`out` 是 `Collector`,不是返回值**。这才是推式的意义:1 进 N 出、1 进 0 出、攒够再吐都自然;`run` 的「进一个返回一个」表达不了。
- **`on_end` 只对有界流保证到达**。攒到结束再吐的逻辑(排序、全局聚合)只能写在这,也就自动意味着**它要求有界输入**(§3.3)。
- **`run` 不废,是 `process` 的特例**:只实现 `run` 的算子,框架用「攒齐全部 → 调一次 `run` → 吐一批」包成 `process`。**不想管流式就别管**,`grep`(§10.1)那种写法一行不用改。

> 和 `kind` 同一原则(§8):**形态是推导的,不是声明的**——只有 `run` ⇒ 阻塞算子(要求有界输入);有 `process` ⇒ 流式算子。

**早停靠的是别把 `_in` 先物化**:上游 chunk 流包成 Arrow `RecordBatchReader`,DuckDB 直接当表扫(还能把投影/过滤下推进去)。于是 `SELECT * FROM _in LIMIT 20` 拉够 20 行就不再拉,reader 关闭 → 取消沿链**反向传播**回 `grep`、再回 source。`search` 的 over-fetch ×2([search.md §4](search.md))在这条链上第一次有了真正意义:多取的那一半只在下游确实要时才被消费。

### 3.3 有界性:上游会不会结束(算子只声明「我要不要有界」)

Flink 最值钱的那个概念:**boundedness 描述的是流,不是算子**。source 决定流有界无界,一路**传播**下去;算子只表明「我需不需要有界输入」。

| 声明位置 | 字段 | 取值 |
|---|---|---|
| 算子吐出的流 | `bounded` | `True`(必然结束)/ `False`(可能永不结束)/ `"inherit"`(随上游,缺省) |
| 算子对输入的要求 | (**推导**,§3.2) | 有 `process` → 不要求;只有 `run` → 要求有界 |

```
search cards "…"     bounded=True      top-k,天生有界
scan cards @asof=…   bounded=True      一张快照,有限行
follow cards         bounded=False     订阅写入流(未来)
sh 'tail -f …'       bounded=False     作者自报;不报就默认 False(EXEC 不可信)
grep / sed / jq      inherit           只过滤/变换,不改变「会不会结束」
```

传播一行话:`bounded(段N) = bounded(段N-1) if inherit else 自己的声明`。

**硬事实:DuckDB 段要求有界输入。** 这不是设计选择,是物理约束——`FROM _in` 需要一个**有限**关系。于是:

```
sh 'tail -f app.log' | SELECT count(*) FROM _in
└─ bounded=False ─────┴─ SQL 段要求有界  ⇒  编译期报错 UnboundedIntoBlocking
```

> **这就是有界性最便宜也最实在的收益:把一类「跑起来才发现永远不返回」变成一条编译期错误。** 没有这个概念,同一条 query 的表现是**静静挂死**——最难查的那种故障。报错时要给出路(换有界 source,或加窗口),不能只报错。

同理,全局 `ORDER BY`/聚合/任何在 `on_end` 里收尾的算子,遇无界输入一律编译期拒;`grep`/`sed`/`jq`/投影逐批出结果,放行。

**执行模式由有界性推导**(Flink 1.12「BATCH 是 STREAMING 的特例」的结论直接搬)——不是三套实现,是**一套推式执行器加两条许可**:

| 管道 | 引擎被允许做什么 |
|---|---|
| 全 `bounded=True` 且全是阻塞算子 | 段边界随便物化 —— **就是今天的 `run` 折叠,零改动** |
| 全 `bounded=True`,链上有流式算子 | 分块推:内存有上界,`LIMIT` 可早停 |
| 任一段 `bounded=False` | **禁止**全量物化;链上出现阻塞算子 = 编译期拒 |

兼容是白送的:今天所有工具都是 `run`-only + 有界 ⇒ 全落在第一行,行为逐字不变。

> **只借这两样,别的不借。** `process`/`Collector` ✅、有界性 ✅;**watermark / event time** ❌(seekbase 没有乱序事件流)、**checkpoint / exactly-once** ❌(单进程交互式查询,失败就重跑;持久性归写侧的 files-first + ticket,见 [store.md](store.md) / [ticket.md](ticket.md))、**keyed state / state backend** ❌(段间状态就是 `_in` 一张表;常驻资源是 §3.1 的服务句柄,不需要分区/快照/rescale)、**timer** ❌(用 `ctx.deadline`/`ctx.cancelled`)、**分布式 shuffle / 并行度** ❌(单进程;并行度是 **DuckDB 段内部**的事,我们不插手)、**窗口** 🕐 挂起(真出现无界 source 再说,届时按 SQL 的 window 语法接,不另造管道 DSL)。
>
> **澄清:as-of ≠ event time,`ds` ≠ watermark。** 这个类比很诱人但错。[time_machine.md](time_machine.md) 的 `ds` 是**行上的数据属性**,as-of 是一条**普通谓词**,下推进候选就完了;watermark 是「事件时间进展到哪」的**流控信号**,用来触发窗口、判迟到。把 `ds` 当 watermark 会平白引入迟到数据 / allowed lateness / side output 一整套**没有对应问题**的机械。
>
> **边界(和 pipeline §2.1 同一条老规矩):算子调度只在接缝之间。** DuckDB 段内部本来就是向量化推式执行的——有自己的 pipeline、morsel 并行、算子链。在它之上再造一层调度器,就是**重造一个更弱的 Flink**,和「把 `WHERE` 拆成 `where` 段」是同一类错误。`process` 管的只是 lance→duck、duck→bash、duck→http 这些**接缝之间的 chunk 流**。

## 4. 格式与 coercion:`TABLE` 是缺省,跨边界才转

stage 之间流动的东西有**格式**。默认是 `TABLE`(一张关系,pipeline §2 的 ABI);为跨进程/跨工具还有几种编码。框架知道它们之间的 **coercion**,在**格式边界**自动插:

| 格式 | 是什么 | 谁用 |
|---|---|---|
| `TABLE` | 活关系(Arrow-backed / DuckDB 视图)——**缺省** | 进程内工具、SQL 段 |
| `ARROW` | Arrow IPC 字节(带类型) | 跨进程、快 |
| `JSONL` | 换行分隔 JSON(人可读) | 跨进程、`jq` 类 CLI |
| `BYTES` | 原始字节 / 文本流 | `sh` 等不透明工具 |
| `ROWS` | 物化行,交回调用方(终端) | sink |
| `NONE` | 空输入(unit) | source 的 `accepts` |

已知 coercion(框架自动):`TABLE ↔ ARROW ↔ JSONL`(互转,经 table)、`TABLE → ROWS`(物化)。`BYTES` **不自动**转 `TABLE`(不透明;要显式 parse 工具)。

- **两个 `TABLE` 段之间零拷**:格式相同,无 coercion(同一 DuckDB 连接挂视图)。这是 `grep` 跟在 `search` 后为什么免费。
- **只有格式不同才序列化**:`search`(emits=TABLE) `| jq`(accepts=JSONL)→ 框架在接缝插 `TABLE→JSONL`(喂 stdin)、`jq` 出来再 `JSONL→TABLE`。这就是 pipeline §4 那笔 marshalling 成本——现在它有了准确的名字:**格式 coercion**,只在格式变的接缝发生。
- **外部命令式作者不碰这个**:你声明 `accepts=JSONL, emits=JSONL`,coercion 框架管;你只写读 stdin JSONL、吐 stdout JSONL 的普通 CLI。
- **恒名 `_in`**:你不需要知道上一段是谁,只认 `_in`。格式匹配(或可 coerce)就能接上,这让工具**可组合、可换位**(`grep` 既能跟在 `search` 后、也能跟在 `read` 后)。

## 5. `ctx`:能力受限的执行上下文(capability-based)

`ctx` 是你和外界之间**唯一**的接口。**ambient authority 一律拒绝**——你不能直接 `open(path)` / `requests.get(url)` / `subprocess.run(...)`,只能调 `ctx` 上**与你声明的 caps 匹配**的 helper;调一个超出 caps 的 helper → `CapabilityViolation`(纵深防御,不只靠编译期)。

| `ctx` 成员 | 给谁用(需声明的 cap) | 说明 |
|---|---|---|
| `ctx.open_read(path)` | `FS_READ` | 只能打开授予的根内的路径;越界即拒 |
| `ctx.open_write(path)` | `FS_WRITE` | 只能写沙箱工作目录 |
| `ctx.http(req)` | `NET` | 走受控出网(可被策略整体禁) |
| `ctx.spawn(argv)` | `EXEC` | 在沙箱里起子进程(限目录/禁网/资源上限) |
| `ctx.embed(text)` / `ctx.tokenize(text)` | (服务白名单) | 给 `search` 这类 source 用的 embedder/分词器 |
| `ctx.asof` | 所有 | 当前 as-of horizon(source 用来下推可见性,见 [time_machine.md](time_machine.md)) |
| `ctx.deadline` / `ctx.cancelled` | 所有 | 超时/取消——长活工具要自觉检查 |

- **`ctx` 里有哪些 helper,由你的 `caps` 决定**:PURE 工具的 `ctx` 没有 `open_read`/`spawn`,想偷偷用也没有;`FS_READ` 工具的 `ctx.open_read` 被钉死在允许的根。**能力即接口**——声明多少、就只拿到多少。
- **为什么强制过 `ctx`**:让「这个工具能干什么」从 caps 声明一眼可查、且运行时强制,而不是埋在 handler 实现里靠人 review。

## 6. 声明 caps:诚实是地基

`caps` 是权限系统的**唯一判据**(tool-registry §3/§6),所以**必须诚实**:

- **就低不就高**:纯表内运算声明 `PURE`,别顺手带 `FS_READ`;同名工具按参数落不同 cap(`grep <pat>` 表内=PURE;`grep <pat> <path>` 读盘=FS_READ)由 `parse_args` 判定后告诉框架。
- **声明不实 = 漏洞**:一个声明 `PURE` 却想联网的工具,`ctx` 里根本没有 `ctx.http`,调用即崩;真要联网就老实声明 `NET`,然后接受它默认受更严策略约束。
- **沙箱兜底**:`EXEC`/`FS_WRITE` 工具即使被策略放行,子进程仍在沙箱里(限目录、禁网、资源上限)——**框架不信你的声明,再加一道墙**(tool-registry §6.3)。

## 7. 输出 schema:下游 SQL 要知道你产了什么列

工具产的表接着被 SQL 段 `FROM _in` 查,所以**列名/类型要可知**。两种方式:

- **静态声明(推荐)**:`out = f(in_schema, args) → schema`。编译期就算出这一段后 `_in` 的 schema,下游 SQL 的列引用能**编译期校验**(引用不存在的列早失败)。`search` 的 `out` = `in? + (pk, _score:double)`。
- **late-bound(动态,慎用)**:像 `sh 'jq …'` 这种输出结构运行时才知道的,schema 只能**执行后从产物推断**(`read_json_auto` 那套)。代价:下游 SQL 的列校验**推迟到运行期**,拼错列名要跑起来才炸。所以能静态声明就静态声明,`sh` 的动态是逃生舱、不是常态。

> 一句话:**静态 schema = 早失败 + 可优化**;late-bound = 灵活但把校验推到运行时。工具作者应尽量给静态 `out`。

## 8. 格式契约:`accepts` / `emits`(位置是推导的,不用 `kind`)

一个 plugin **不声明自己是 source/tool/sink**——它只声明**能收哪些输入格式(`accepts`)、吐哪种输出格式(`emits`)**。它在管道里能放哪、算不算 source,全从格式**推导**:

| 你声明 | 推导出的角色 | 位置 | 例 |
|---|---|---|---|
| `accepts={NONE}` | **source**(无上游) | 只能打头 | `search` `scan` `read` |
| `accepts={TABLE}`,`emits=TABLE` | 中间工具 | 中间 | `grep` `sed` `embed` |
| `accepts={JSONL}`,`emits=JSONL` | 中间工具(跨进程) | 中间 | `jq` `sh` |
| `emits=ROWS` | **sink**(终端输出) | 只能收尾 | `emit`(默认末端) |

- **`kind` 是多余的**:source = 「`accepts` 含 `NONE`」、sink = 「`emits` 是 `ROWS`」——都能从格式读出来,不必再手工贴一个可能和格式**打架**的标签(声明 `kind=source` 却 `accepts=TABLE` 就是自相矛盾;去掉 `kind`,这种矛盾根本不存在)。
- **格式匹配即可组合**:`A | B` 合法 ⟺ `emits(A) ∈ accepts(B)`,或存在已知 coercion 把 `emits(A)` 转成 `accepts(B)` 里的某格式(§4)。无路可转 → **编译期格式不匹配报错**(早失败)。
- **多格式 = 更表达力**:一个工具可 `accepts={TABLE, JSONL}`,让框架挑最省的那条(省一次 coercion)。这是单维度 `kind` 给不了的——**位置是一个维度,格式是一组**。
- **source 可读 `ctx.asof`**:`accepts={NONE}` 的工具把时光机 as-of 下推进自己的候选(`search` 见 [search.md §6](search.md),`scan` 见 [time_machine.md](time_machine.md))。
- **transform ≠ plugin**:一整条 DuckDB SQL(概念上 `accepts=TABLE, emits=TABLE`)是管道缺省(pipeline §2.1),不进 registry、不用声明——首 token 不命中 registry 的段就是 SQL。

> **三个正交的轴,没有一个是 `kind`**——各回答一个不同问题,互不替代:
>
> | 轴 | 声明 | 回答 | 决定 |
> |---|---|---|---|
> | **格式契约** | `accepts` / `emits` | 我能接谁? | 组合合法性 + coercion(§4/§8) |
> | **常驻状态** | 有无 `start`/`stop` | 要不要复用贵资源? | 生命周期(§3.1) |
> | **流动性 / 有界性** | 有无 `process` + `bounded` | 数据怎么流、会不会结束? | 执行模式 + 早停 + 编译期拦截(§3.2/§3.3) |
>
> ```
> grep    = TABLE→TABLE   + 无状态      + process, inherit        流式、透传有界性
> search  = NONE →TABLE   + 服务型      + run,     bounded=True   阻塞(top-k 一次算完)但天生有界
> follow  = NONE →TABLE   + 服务型      + process, bounded=False  唯一真·无界的那类
> jq      = JSONL→JSONL   + 无状态      + process, inherit        子进程流式对拷,不再全量攒
> SQL 段  = TABLE→TABLE   + (非 plugin) + 要求有界输入            §3.3 的硬约束
> ```
>
> 作者的默认路径**没变**:只写 `run`、两档都不填 ⇒ 无状态 + 阻塞 + `inherit`,和今天逐字一致。`start`/`stop` 和 `process`/`bounded` 是**给需要的人用的第二档**。

## 9. 注册:挂进 registry

```python
db = await Seekbase.open("./data", schema=SCHEMA, tools=[
    Tool(name="grep", accepts={Fmt.TABLE}, emits=Fmt.TABLE, args="<pattern> [--field <col>]",
         caps={Cap.PURE}, out=grep_out_schema, run=grep_run),
    SearchSource(),          # 类式 plugin 实例(服务型:带 start/run/stop,§10.2)
    ExternalTool("jq", argv=["jq", "-c", "{arg0}"],
                 accepts={Fmt.JSONL}, emits=Fmt.JSONL, caps={Cap.EXEC}),
])
```

- **名字规则**:不取 SQL 引导关键字(`select`/`with`/`from`…),否则会和「SQL 缺省」相撞(tool-registry §5);和内建/已注册**同名 → 显式报错**,不覆盖。
- **内建 + 用户注册同一张表**:你的工具和 `search` 平权;用户工具**必须声明 caps**,进不了「审过」名单、默认按声明 caps 受策略约束 + 沙箱。

## 10. 三个完整例子(从最简到最复杂)

### 10.1 `grep`(最简:无状态、无常驻进程)—— 表内正则过滤

一次性纯函数,**没有 `start`/`stop`、什么都不留**——这是最简单的一类工具:

```python
def grep_out_schema(in_schema, args):
    return in_schema                                   # 只过滤行,列不变

def grep_run(in_table, args, ctx):                     # PURE:ctx 只有 asof/deadline,无 io
    col = args.get("field")                            # None = 所有文本列
    pat = re.compile(args["pattern"])
    return in_table.filter(lambda row: any(            # 拿到 _in 就地过滤,调用完即弃
        pat.search(str(row[c])) for c in (col and [col] or in_table.text_cols)))

Tool(name="grep", accepts={Fmt.TABLE}, emits=Fmt.TABLE, args="<pattern> [--field <col>]",
     caps={Cap.PURE}, out=grep_out_schema, run=grep_run)      # TABLE→TABLE,只注册一个 run
```

用:`search cards '事故' | grep 'ERROR' --field issue | SELECT card_id FROM _in LIMIT 20`。**没有任何常驻状态**:每次调用自给自足,关库时也无从拆起。

### 10.2 `search`(最复杂:服务型、有常驻进程)—— start / run / stop

`search` 复杂**不在算法**(RRF 就那样),而在**它是个服务**:背后一个常驻的向量引擎 + 载进 RAM 的 HNSW 索引 + embedder 连接池,**开一次、每次 `run` 复用**。所以它实现完整的 `start`/`run`/`stop`:

```python
class SearchSource:                                    # 类式:实例持有常驻 handle
    name  = "search"
    accepts, emits = {Fmt.NONE}, Fmt.TABLE             # NONE→TABLE ⇒ 推导为 source(§8)
    caps  = {Cap.PURE}                                 # 引擎在库内、检索不碰外界 = PURE
                                                       # (embedder 是注入的常驻服务,自身的 NET 由 ctx 中介)
    def out_schema(self, _in, args):                   # accepts=NONE:in_schema 为 None
        return Schema([("pk","str"), ("_score","double")]) + hit_cols(args["table"])

    async def start(self, ctx):                        # ★ open 时一次:拉起常驻服务(重活)
        self.engine = await ctx.open_engine("vss")     #   开向量引擎连接 + 把 HNSW 载进 RAM
        self.embed  = ctx.embedder                     #   常驻 embedder 客户端(连接池)

    def run(self, in_table, args, ctx):                # ★ 每次调用:复用 self.engine,绝不重开
        qvec = self.embed(args["text"])                #   embed 查询
        qtok = ctx.tokenize(args["text"])              #   jieba 分词
        hits = self.engine.hybrid(args["table"], qvec, qtok,   # RRF,as-of 下推(见 search.md §3/§6)
                                  k=args.get("k", 10), asof=ctx.asof)
        return hits_to_table(hits, self.out_schema(None, args))

    async def stop(self):                              # ★ close 时一次:拆常驻服务
        await self.engine.close()                      #   关连接、释放索引内存
```

用:`search cards 'pty 终端' | SELECT * FROM _in WHERE kind='issue' ORDER BY _score DESC LIMIT 20`。

**和 `grep` 的对照就是这一节的重点**:

| | `grep`(无状态) | `search`(服务型) |
|---|---|---|
| 常驻进程 | 无 | 有:引擎连接 + RAM 里的 HNSW + embedder 池 |
| 生命周期 | 只 `run` | `start`(开一次)/ `run`(复用)/ `stop`(拆) |
| 每次调用成本 | 就地算,自给自足 | 复用常驻 handle;**重活在 `start`,不在 `run`** |
| 关库 | 无事可做 | 必须 `stop` 释放索引内存 / 关连接 |

> 关键不变量:**`run` 里绝不 `open_engine`**——那是 `start` 的活。把「开一次的贵资源」错放进 `run`,就是每次查询都重载一遍索引,服务型工具的意义全丢。

### 10.3 `jq`(外部命令式,EXEC/沙箱)—— 包一个 CLI

```python
ExternalTool("jq", argv=["jq", "-c", "{arg0}"],              # {arg0} = 用户传的 jq 脚本
             accepts={Fmt.JSONL}, emits=Fmt.JSONL,           # jq 认 JSONL 进、JSONL 出
             caps={Cap.EXEC})                                # 框架自动 coerce 上游 TABLE→JSONL、下游 JSONL→TABLE(§4)
```

用:`search cards '事故' | jq 'select(.severity>=3)' | SELECT kind, count(*) FROM _in GROUP BY kind`。默认 `read-only` 策略下 **`jq` 因 `EXEC` 被拒**,要显式升级到 `sandboxed`/`trusted` 才能跑(tool-registry §6)——作者无需为权限操心,策略层统一管。子进程本身是每次调用起一个(外部命令式**无常驻**,和服务型的进程内 handle 不同)。

## 11. 生命周期:register → (start) → resolve → authorize → execute → (stop)

```
open(tools=[…])          注册进 registry(name 不撞、caps 记下);服务型工具 start(ctx) 拉起常驻服务(开引擎/载索引)
   │
parse 管道               解析:每段首 token 命中 registry?→ 是=工具 / 否=SQL 缺省
   │
compile-time authorize   授权:工具 caps vs 策略 → allow / ask / deny(deny→编译期拒,管道不启动)
   │
execute(fold _in)        执行:tool.run(_in, args, ctx);服务型复用常驻 handle,无状态每次自给自足
   │
close                    服务型工具 stop(handle):关连接、释放索引内存;无状态工具无事可做
```

作者管 `run`(服务型再加 `start`/`stop`);resolve / authorize / 沙箱 / 生命周期编排都是框架的活——它在 `open` 时替你调 `start`、`close` 时调 `stop`。

## 12. 测试一个 tool

- **单测 `run`**:喂一张构造的 Arrow 表 + 一个 fake `ctx`(按 caps 只放对应 helper),断言输出表的 schema + 行。
- **服务型工具测生命周期**:`start` 后断言 handle 建好、`run` **复用**同一 handle(不重开引擎——可用 mock 引擎断言 `open` 只调一次)、`stop` 后资源释放。这是 `search` 这类工具比 `grep` 多出来的测试面。
- **流式算子测 `process`**:喂多批 chunk,断言输出**不依赖批边界**(同样的行,拆 1 批 / 拆 5 批结果一致);断言 `on_end` 之前不吞行、之后 flush 干净;断言中途 `ctx.cancelled` 能提前收手。
- **有界性测编译期**:`bounded=False` 的 source 接一条 SQL 段,断言**编译期**报 `UnboundedIntoBlocking`(而不是跑起来挂住——这条测试的全部意义就是防它退化回挂死)。
- **caps 负测**:给 PURE 工具的 fake `ctx` 不放 `open_read`,断言它没偷偷读盘(调了就 `CapabilityViolation`)。
- **管道集成**:`db.query("<tool> … | SELECT …")` 跑通,验证下游 SQL 能引用你声明的 `out` 列;late-bound 工具额外验证运行期 schema。
- **策略测**:`read-only` 下 `EXEC`/`NET` 工具被 deny;升级后放行且仍在沙箱内。

## 13. 诚实的代价 / 边界

- **输出 schema 是契约**:一旦有下游 SQL 依赖你的 `out` 列,改列名/类型 = 破坏性变更。late-bound 工具把这份契约推到运行时,更脆——所以尽量静态。
- **服务型工具 = 常驻内存 + 生命周期负担**:`search` 的引擎连接 + HNSW 索引常驻 RAM(天花板见 [search.md §5](search.md)),绑在 `open`/`close` 上——起得慢(`start` 载索引)、占内存、忘了 `stop` 会漏。无状态工具(`grep`)零常驻,没这些账。**够用就别做成服务型**:只有「每次调用要复用一份开一次的贵资源」才值得背 start/stop。
- **进程内工具阻塞事件循环**:函数式 `run` 是同步的,重活要走 bridge/线程(和 DuckDB 一样,见 [concurrency.md](concurrency.md)),否则卡住 async 门面。服务型的 `start`/`stop` 是 async(开/关引擎是 io)。
- **外部命令式的 marshalling 成本**:每进出一次子进程,表就序列化一次(pipeline §9);热路径别滥用 `sh`,能用进程内 PURE 工具就用。
- **今天 seekbase 没有一个无界 source**。所以 §3.3 现在的收益只有一条:`sh 'tail -f'` 这类从「挂死」变成编译期报错。值不值取决于你信不信「管道能串 shell ⇒ 迟早有人串 `tail -f`」——我认为会,而**挂死是最贵的一类故障**,一个布尔字段换掉它很便宜。
- **`process` 比 `run` 难写**:攒批、`on_end` flush、自觉检查 `ctx.cancelled` 都是心智负担。所以它**不是默认**——`run` 永远够用且永远受支持,只有真需要内存上界/早停的算子才升级。
- **流式 `_in` 只能被扫一次**:SQL 段里 `_in` 出现两次(自连接、`WITH` 引用两遍)必须退回物化(编译期数引用数,>1 就物化),而这**只在有界流上允许**。这是把 temp table 换成 Arrow reader 的直接代价。
- **早停让统计不准**:`LIMIT` 取消上游后,`search` 的 over-fetch、`grep` 的扫描行数都是**部分**的——「扫了多少行」这类指标必须标注「被取消」,否则读数的人会误判。
- **推式不带来跨段优化**:`LIMIT` 能反压回去,`WHERE` 仍然**不会**下推进 `search`(pipeline §9 那堵优化墙还在)。谓词下推要靠 source 段自己吃谓词,不是靠算子模型。
- **不做 exactly-once**:管道中途失败 = 整条重跑。读路径无副作用所以安全;但 `FS_WRITE`/`NET` 工具**重跑就是重复副作用**——幂等性归作者,框架不担保。
- **caps 可信度**:整套授权建立在「作者诚实声明 caps」上;沙箱兜 `EXEC`/`FS_WRITE`,但弱平台上沙箱退化,此时靠策略 `deny` 名单硬关(tool-registry §8)。
- **不管数据权限**:plugin 契约管「工具能碰哪些资源」,**不**管「这次调用能看表 T 的哪些行」(行级授权是另一层)。

## 14. 与其他文档

- [tool-registry.md](tool-registry.md):系统视角——registry、caps 分级、能力×策略权限、沙箱(本文是它的作者侧)。
- [pipeline-as-anything.md](pipeline-as-anything.md):`_in` 表 ABI、source/transform/tool/sink、SQL 缺省、段间序列化。
- [search.md](search.md):内建 `search` source 就是一个 plugin(可插拔引擎),可当范例;§4 的 over-fetch ×2 在早停链上的意义见 §3.2。
- [time_machine.md](time_machine.md):`ctx.asof` 语义,source 工具如何下推可见性;**`ds` 不是 watermark**(§3.3 澄清)。
- [concurrency.md](concurrency.md):进程内 `run` 为什么重活要下沉到 bridge/线程——`process` 的每批调用同样是阻塞的,同一条规矩。
