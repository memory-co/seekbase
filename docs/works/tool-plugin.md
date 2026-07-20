# tool-plugin — 写一个工具:可插拔**算子**的契约与实现

> 状态:**设计稿(pipeline 方向,未落)**。[tool-registry.md](tool-registry.md) 讲**系统视角**(registry 怎么存、权限怎么判);本文讲**作者视角**:你要给 seekbase 加一个管道工具(`search` 是内建的一个、`grep`/`find`/`sh` 是另几个),得实现一个什么样的 **plugin**?
>
> **一个 plugin = 一个算子(operator)。** 管道就是一串算子,框架只定**算子 ABI**,谁都能往里插——`search`、`grep`、`jq`、以及 SQL 段,在 ABI 面前是同一种东西。
>
> **但 seekbase 不自己跑管道**:整条管道被**降级**成 DuckDB 的 `WITH` 链或 bash 的 pipeline([pipeline-runtime-optimize.md](pipeline-runtime-optimize.md))。所以算子的**首要**作业不是「怎么算」,而是**「我在某个宿主里长什么样」**——`native_duckdb` / `native_bash`(§3.2)。两边都不会写,才退回 Python 的 `run`,由框架包成 vtab(能跑,最贵)。


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

作者要填的就这几格,重点是 **`accepts`/`emits`**(格式契约,§8)、**`run`**(§3)、**`caps`**(§6)、**`out`**(§7);另有两档**按需才填**的:**服务型工具**(背后有常驻进程,如 `search`)加 **`start`/`stop`** 生命周期(§3.1);**想跑得便宜**的工具再加 **`native_duckdb` / `native_bash`**——告诉编译器自己在宿主里长什么样,不填就走保底的 vtab(§3.2)。`grep` 这类最简工具两档都不填也能跑,只是贵。

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

### 3.2 native 降级:`native_duckdb` / `native_bash`(**首要**作业)

管道不由 seekbase 执行,而是被编译成 **DuckDB `WITH` 链**或 **bash pipeline**([pipeline-runtime-optimize.md](pipeline-runtime-optimize.md))。所以算子最该交的作业,是**告诉编译器自己在宿主里长什么样**:

```python
Tool(
    name = "grep", accepts={Fmt.TABLE}, emits=Fmt.TABLE, caps={Cap.PURE},
    native_duckdb = lambda prev, a: f"SELECT * FROM {prev} WHERE regexp_matches({a.field}, {a.pat!r})",
    native_bash   = lambda a: ["grep", "-E", a.pat],
    run           = grep_run,     # 保底:两版 native 都没有时,框架包成 vtab(能跑,最贵)
)
```

三格**都可选**,填得越多编译器可挑的越多:

| 你填了 | 后果 |
|---|---|
| 只有 `run` | 永远被包成 vtab;能跑,**最贵**(优化屏障 + 每批 marshal) |
| 一版 `native*` | 在那个宿主里**免费**;在另一个宿主里是**切换点**(整条管道被劈开) |
| 两版 `native*` | **永不成为切换点**——跟着上下文走,零成本 |

> **写第二版 native 的理由不是「能在两边跑」,是「不成为切换点」。** 一堆 duck 段中间夹一个只会 bash 的 `grep`,代价是整条管道劈成三截 + 一座 vtab 桥;给它一版 `native_duckdb`(把 grep 能力整个翻译成 `WHERE`),代价直接归零。收益是复利的,见 [pipeline-runtime-optimize.md §5](pipeline-runtime-optimize.md)。

**两版必须语义等价**——同一条 query 因为编译器选了不同宿主而结果不同,是这套设计最危险、最难查的一类 bug。`regexp_matches`(RE2)和 `grep -E`(POSIX ERE)在反向引用、lookahead、字符类上**并不一致**,所以双 native 算子的准入条件是 **differential test**(同一输入强制走两边,断言逐行一致),不是可选测试。

`run` 里的 Python 是保底路径:框架把它包成 duck 侧的 vtab(批回调)或 bash 侧的子命令。`grep`(§10.1)那种只写 `run` 的形态**一行都不用改**,只是跑得贵。

### 3.3 有界性:一条规则,不是一套机制

Flink 那个最值钱的概念——**boundedness 描述的是流,不是算子**——在这里**几乎不用实现**:因为管道降级到宿主(§3.2),有界性就是**宿主自带的性质**。

| 宿主 | 有界性 | 为什么 |
|---|---|---|
| DuckDB `WITH` 链 | **必然有界** | `FROM` 需要有限关系,物理约束 |
| bash pipeline | **可无界** | `tail -f \| grep …` 是内核管道的日常 |

于是只剩**一条**编译期检查(不需要 `bounded` 字段、不需要传播算法):

```
sh 'tail -f app.log' | SELECT count(*) FROM _in
└─ bash 宿主,无界 ────┴─ duck 宿主  ⇒  编译期报错:无界流不能进 duck
```

> **收益全留下、成本几乎归零:把一类「跑起来才发现永远不返回」变成编译期错误。** 没有这条检查,这种 query 的表现是**静静挂死**——最难查的那类故障;有了它,只需问「这一段落在哪个宿主」。报错要给出路(换有界 source,或让整条留在 bash 宿主),不能只报错。
>
> 唯一需要作者声明的:**只有 `bash` 宿主的 source 要自报会不会结束**(`sh` 不报就按无界处理——`EXEC` 不可信)。其余算子什么都不用填。

**只借这两样(`native` 降级的思路 + 有界性),别的不借。** **watermark / event time** ❌(seekbase 没有乱序事件流)、**checkpoint / exactly-once** ❌(单进程交互式查询,失败就重跑;持久性归写侧的 files-first + ticket,见 [store.md](store.md) / [ticket.md](ticket.md))、**keyed state / state backend** ❌(段间状态就是 `_in` 一张表;常驻资源是 §3.1 的服务句柄,不需要分区/快照/rescale)、**timer** ❌(用 `ctx.deadline`/`ctx.cancelled`)、**分布式 shuffle / 并行度** ❌(单进程;并行度是 **DuckDB 段内部**的事)、**窗口** 🕐 挂起(真出现无界 source 再说,届时按 SQL 的 window 语法接,不另造管道 DSL)。

> **澄清:as-of ≠ event time,`ds` ≠ watermark。** 这个类比很诱人但错。[time_machine.md](time_machine.md) 的 `ds` 是**行上的数据属性**,as-of 是一条**普通谓词**,下推进候选就完了;watermark 是「事件时间进展到哪」的**流控信号**,用来触发窗口、判迟到。把 `ds` 当 watermark 会平白引入迟到数据 / allowed lateness / side output 一整套**没有对应问题**的机械。
>
> **边界(和 pipeline §2.1 同一条老规矩):我们不造调度器。** DuckDB 段内部本来就是向量化推式执行(自己的 pipeline、morsel 并行、算子链),bash pipeline 本来就有内核背压。在它们之上再造一层算子调度,就是**重造一个更弱的 Flink**,和「把 `WHERE` 拆成 `where` 段」是同一类错误。算子只负责**告诉编译器自己在宿主里长什么样**,跑是宿主的事。

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
> | **宿主降级** | 有哪几版 `native_*` | 我能编译进哪个宿主? | 宿主指派 + 会不会成为切换点(§3.2) |
>
> ```
> grep    = TABLE→TABLE   + 无状态      + native: duck & bash    永不成为切换点
> search  = NONE →TABLE   + 服务型      + native: duck & bash    duck 版走官方集成,bash 版走 SDK
> jq      = JSONL→JSONL   + 无状态      + native: bash           在 duck 宿主里是切换点(架 vtab)
> 自写工具 = TABLE→TABLE   + 无状态      + native: 无             永远包 vtab:能跑,最贵
> SQL 段  = TABLE→TABLE   + (非 plugin) + 天生 duck               整条链的锚点
> ```
>
> 作者的最低门槛**没变**:只写 `run`、两档都不填 ⇒ 无状态 + 保底 vtab,照样能跑。`start`/`stop` 和 `native_*` 都是**为了跑得便宜才填的第二档**。

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
- **双 native 的 differential test(准入条件,非可选)**:同一份输入,分别**强制**走 `native_duckdb` 和 `native_bash`,断言逐行一致;再和保底 `run` 也比一遍。三条路结果不同 = 同一条 query 换宿主换答案,这是本设计最危险的 bug(§3.2)。
- **无界测编译期**:bash 宿主的无界 source 接一条 SQL 段,断言**编译期**报错(而不是跑起来挂住——这条测试的全部意义就是防它退化回挂死,§3.3)。
- **降级测 `EXPLAIN`**:断言编译出的宿主指派符合预期(比如「加了 `native_duckdb` 之后切换点从 2 变 0」),否则性能回退无人察觉。
- **caps 负测**:给 PURE 工具的 fake `ctx` 不放 `open_read`,断言它没偷偷读盘(调了就 `CapabilityViolation`)。
- **管道集成**:`db.query("<tool> … | SELECT …")` 跑通,验证下游 SQL 能引用你声明的 `out` 列;late-bound 工具额外验证运行期 schema。
- **策略测**:`read-only` 下 `EXEC`/`NET` 工具被 deny;升级后放行且仍在沙箱内。

## 13. 诚实的代价 / 边界

- **输出 schema 是契约**:一旦有下游 SQL 依赖你的 `out` 列,改列名/类型 = 破坏性变更。late-bound 工具把这份契约推到运行时,更脆——所以尽量静态。
- **服务型工具 = 常驻内存 + 生命周期负担**:`search` 的引擎连接 + HNSW 索引常驻 RAM(天花板见 [search.md §5](search.md)),绑在 `open`/`close` 上——起得慢(`start` 载索引)、占内存、忘了 `stop` 会漏。无状态工具(`grep`)零常驻,没这些账。**够用就别做成服务型**:只有「每次调用要复用一份开一次的贵资源」才值得背 start/stop。
- **进程内工具阻塞事件循环**:函数式 `run` 是同步的,重活要走 bridge/线程(和 DuckDB 一样,见 [concurrency.md](concurrency.md)),否则卡住 async 门面。服务型的 `start`/`stop` 是 async(开/关引擎是 io)。
- **外部命令式的 marshalling 成本**:每进出一次子进程,表就序列化一次(pipeline §9);热路径别滥用 `sh`,能用进程内 PURE 工具就用。
- **双 native = 双份维护 + 等价性风险**(§3.2)。只写一版是完全正当的选择,它只意味着这个算子**是个切换点**——请在文档里写明,别让用户猜为什么某条管道突然变慢。
- **只写 `run` 不是错,是贵**:保底 vtab 一定跑得通,但它是**优化屏障**(DuckDB 看不穿桥、谓词推不进去、基数估计失真)+ 每批 marshal。热路径上的算子值得补一版 native。
- **今天 seekbase 没有一个无界 source**。所以 §3.3 那条检查现在的收益只有:`sh 'tail -f'` 从「挂死」变成编译期报错。值不值取决于你信不信「管道能串 shell ⇒ 迟早有人串 `tail -f`」——我认为会,而**挂死是最贵的一类故障**,而且这条检查现在几乎不要钱(只问宿主,不做传播)。
- **可解释性变差**:用户写 5 段管道,跑的是 1 条 SQL + 1 个子进程。报错行号、性能归因都要能映射回用户写的那一段,否则不可用——所以 `EXPLAIN`(打印宿主指派 + 每个切点)是必需品,不是奢侈品。
- **不做 exactly-once**:管道中途失败 = 整条重跑。读路径无副作用所以安全;但 `FS_WRITE`/`NET` 工具**重跑就是重复副作用**——幂等性归作者,框架不担保。
- **caps 可信度**:整套授权建立在「作者诚实声明 caps」上;沙箱兜 `EXEC`/`FS_WRITE`,但弱平台上沙箱退化,此时靠策略 `deny` 名单硬关(tool-registry §8)。
- **不管数据权限**:plugin 契约管「工具能碰哪些资源」,**不**管「这次调用能看表 T 的哪些行」(行级授权是另一层)。

## 14. 与其他文档

- [tool-registry.md](tool-registry.md):系统视角——registry、caps 分级、能力×策略权限、沙箱(本文是它的作者侧)。
- [pipeline-as-anything.md](pipeline-as-anything.md):`_in` 表 ABI、source/transform/tool/sink、SQL 缺省、段间序列化。
- [pipeline-runtime-optimize.md](pipeline-runtime-optimize.md):**本文 `native_*` 的去处**——宿主指派、融合切段、vtab 桥、代价阶梯。作者视角看「填几版 native」,系统视角看「编译器怎么用它们省钱」。
- [search.md](search.md):内建 `search` 是个双 native 算子(bash 版调 LanceDB SDK / duck 版走官方集成)。
- [time_machine.md](time_machine.md):`ctx.asof` 语义,source 工具如何下推可见性;**`ds` 不是 watermark**(§3.3 澄清)。
- [concurrency.md](concurrency.md):进程内 `run` 为什么重活要下沉到 bridge/线程(保底 vtab 的批回调同理)。
