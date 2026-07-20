# operator-plugin — 写一个算子:`Operator` 基类的契约与实现

> 状态:**设计稿(pipeline 方向,未落)**。[operator-registry.md](operator-registry.md) 讲**系统视角**(registry 怎么存、权限怎么判);本文讲**作者视角**:你要给 seekbase 加一个管道算子(`search` 是内建的一个、`grep`/`find`/`sh` 是另几个),得实现一个什么样的 **plugin**?
>
> **一个 plugin = 一个算子(operator)= 一个 `Operator` 子类。** 管道就是一串算子,框架只定**算子基类**,谁都能继承它插进来——`search`、`grep`、`jq` 在基类面前是同一种东西。
>
> **但 seekbase 不自己跑管道**:整条管道被**降级**成 DuckDB 的 `WITH` 链或 bash 的 pipeline([pipeline-runtime-optimize.md](pipeline-runtime-optimize.md))。所以算子的**首要**作业不是「怎么算」,而是**「我在某个宿主里长什么样」**——覆写 `compile_duck` / `compile_bash`(§3.2)。两个都不覆写,才退回 Python 的 `run`,由框架包成 vtab(能跑,最贵)。
>
> 依赖:[operator-registry.md](operator-registry.md)(registry、caps 分级、能力×策略权限)、[pipeline-as-anything.md](pipeline-as-anything.md)(`_in` 表 ABI、SQL 是缺省)。

## 1. 定位:一个算子 = 一个 `Operator` 子类

管道里每段非-SQL 的 verb 都由一个 plugin 支撑。**框架不认识 `search` 也不认识 `grep`,它只认识 `Operator`**——`search` 和你自己写的算子继承的是同一个基类(这就是「可插拔算子机制」的全部意思)。一个 plugin 就是**一个子类**:

```python
class Grep(Operator):
    name    = "grep"
    accepts = {Fmt.TABLE}                     # 能收哪些输入格式(source 用 {Fmt.NONE},§8)
    emits   = Fmt.TABLE                       # 吐哪种输出格式
    caps    = {Cap.PURE}                      # 诚实声明碰什么外界资源(§6)

    class Args(ArgSpec):                      # 参数签名:供解析 + --help
        pattern: str
        field:   str | None = None

    def out_schema(self, in_schema, args):    # 输出表的列(§7)
        return in_schema                      #   只过滤行,列不变

    def compile_duck(self, prev, args):    # 我在 duck 宿主里长这样(§3.2)
        return f"SELECT * FROM {prev} WHERE {self._sql_pred(args)}"

    def compile_bash(self, args):          # 我在 bash 宿主里长这样
        return ["grep", "-E", args.pattern]
```

**声明放类属性,行为放方法覆写。** 该覆写哪些、可以不覆写哪些,见 §2。

> **不用 `kind`**:算子不声明自己是 source/external/sink——它只声明**收什么格式、吐什么格式**,是不是 source(无输入)、sink(终端输出)由格式**推导**(§8)。分类是格式的投影,不是另一个要维护的字段。

## 2. 一个基类,几处覆写点

```python
class Operator(ABC):
    # ── 声明:类属性 ──────────────────────────────────────────
    name:    ClassVar[str]
    accepts: ClassVar[set[Fmt]] = {Fmt.TABLE}
    emits:   ClassVar[Fmt]      = Fmt.TABLE
    caps:    ClassVar[set[Cap]] = {Cap.PURE}
    class Args(ArgSpec): ...                          # 内嵌参数签名

    def parse_args(self, tokens) -> Args: ...         # 可覆写:自定义校验 / 按参数推导 caps(§6)

    # ── schema:必须实现 ──────────────────────────────────────
    @abstractmethod
    def out_schema(self, in_schema, args) -> Schema: ...

    # ── 宿主降级:至少覆写一个,否则回落 run(§3.2)───────────
    def compile_duck(self, prev, args) -> str:      raise NotSupported
    def compile_bash(self, args) -> list[str]:      raise NotSupported

    # ── 保底:两个 compile_* 都没有时走这条(被包成 vtab)────────
    def run(self, in_data, args, ctx):                raise NotSupported

    # ── 生命周期:服务型才覆写(§3.1)────────────────────────
    async def start(self, ctx) -> None: pass
    async def stop(self) -> None:       pass
```

加两个**只预设默认值**的薄子类,不是新概念:

| 基类 | 预设了什么 | 你还要写什么 |
|---|---|---|
| `Operator` | 什么都不预设 | 全部 |
| `Source(Operator)` | `accepts = {Fmt.NONE}` | 其余照旧(§8 会把它推导成 source) |
| `ExternalCommand(Operator)` | `caps = {Cap.EXEC}`,并**替你实现 `compile_bash`**(按 `argv` 模板渲染) | 只填 `argv` + 格式 |

> **层次是浅的,只有一层。** 不搞 `AbstractBaseFilterOperatorFactory` 那套;`Source` / `ExternalCommand` 就是省几行样板,你随时可以直接继承 `Operator` 自己写。

**为什么是继承,而不是「填一条记录 + 几个 lambda」**——记录式撑不住稍微复杂一点的算子:

- **状态没处放**。服务型算子要在 `start` 里开引擎、把 handle 留到每次调用复用(§3.1)。没有 `self`,handle 只能塞模块级全局——多实例(两个后端、两个库)直接崩。
- **覆写点之间无法共享逻辑**。`compile_duck`、`compile_bash`、`run` 三条路要产生**同一个语义**(§3.2),它们必然共用参数校验、谓词构造、列名推导。三个独立函数只能靠模块级 helper 或复制粘贴;有 `self` 就是一个私有方法。
- **不能复用**。`search` 的 lance 后端和 vss 后端 90% 相同(参数、schema、RRF 语义),差的只是降级实现——那天然是**一个父类 + 两个子类**(§10.2)。记录式只能复制两份,然后它们慢慢长歪。
- **不能按参数变行为**。`grep <pat>` 是 `PURE`、`grep <pat> <path>` 是 `FS_READ`(§6);这要覆写 `parse_args`,记录里无处覆写。
- **没法测**。服务型的 `start`/`run`/`stop` 要断言「handle 只开一次」,得有实例才能测(§12)。

## 3. 核心契约

三条降级路径 + 一条保底,**语义必须一致**;加上一对生命周期钩子。逐条看:

### 3.1 无状态 vs 服务型:要不要常驻进程

按**背后有没有常驻状态**分两类——**这正是 `grep` 简单、`search` 复杂的根源**:

- **无状态算子(如 `grep`)**:**没有常驻进程**,每次调用自给自足,调用完什么都不留。`start`/`stop` **不覆写**(基类的空实现就对),框架看到没覆写就当它零常驻。
- **服务型算子(如 `search`)**:背后是一个**常驻服务**——向量引擎连接、**载进 RAM 的 HNSW 索引**、embedder 连接池。这些**开一次、复用多次**,**绝不能每次调用都重开**:

  | 钩子 | 何时 | 干什么 |
  |---|---|---|
  | `start(ctx)` | `open` 时一次 | 拉起常驻服务:开引擎、载索引进 RAM、暖 embedder;**存进 `self`** |
  | `run` / `compile_*` | 每次调用 | **复用 `self` 上的 handle**,绝不重开 |
  | `stop()` | `close` 时一次 | 拆:关连接、释放索引内存 |

> 分界线:**「每次调用要不要复用一份贵的、开一次的资源」**。要 → 覆写 `start`/`stop`;不要 → 别覆写。`search` 的引擎 + RAM 常驻索引就是那份贵资源,`grep` 什么都不用留。
>
> **这也是必须用类的最直接原因**:handle 存 `self`,天然支持一个进程里跑多个实例(两个后端 / 两个库),而模块级全局做不到。

### 3.2 宿主降级:`compile_duck` / `compile_bash`(**首要**作业)

管道不由 seekbase 执行,而是被编译成 **DuckDB `WITH` 链**或 **bash pipeline**([pipeline-runtime-optimize.md](pipeline-runtime-optimize.md))。所以算子最该交的作业,是**告诉编译器自己在宿主里长什么样**——`compile_*` **在编译期产代码**(返回 SQL 文本 / argv,不碰数据),`run` **在运行期真干活**(拿 `_in` 算出结果):

```python
class Grep(Operator):
    def _regex(self, args):                    # ★ 三条路共用一份参数逻辑(记录式做不到)
        return args.pattern

    def compile_duck(self, prev, args):        # 编译期 → 一段 SQL 文本(duck 宿主:翻成 WHERE)
        col = args.field or "*"
        return f"SELECT * FROM {prev} WHERE regexp_matches({col}, {self._regex(args)!r})"

    def compile_bash(self, args):              # 编译期 → 一段 argv(bash 宿主:就是 grep 本身)
        return ["grep", "-E", self._regex(args)]

    def run(self, in_table, args, ctx):        # 运行期 → 就地算(保底:被包成 vtab,能跑最贵)
        pat = re.compile(self._regex(args))
        return in_table.filter(lambda r: pat.search(str(r[args.field])))
```

**覆写哪几个决定你贵不贵**——由框架**检测覆写**得出(不用声明,和 `kind` 同一原则,§8):

| 你覆写了 | 后果 |
|---|---|
| 只有 `run` | 永远被包成 vtab;能跑,**最贵**(优化屏障 + 每批 marshal) |
| 一个 `compile_*` | 在那个宿主里**免费**;在另一个宿主里是**切换点**(整条管道被劈开) |
| 两个 `compile_*` | **永不成为切换点**——跟着上下文走,零成本 |

> **写第二个 compile_* 的理由不是「能在两边跑」,是「不成为切换点」。** 一堆 duck 段中间夹一个只会 bash 的 `grep`,代价是整条管道劈成三截 + 一座 vtab 桥;覆写一个 `compile_duck`(把 grep 能力整个翻译成 `WHERE`),代价直接归零。收益是复利的,见 [pipeline-runtime-optimize.md §5](pipeline-runtime-optimize.md)。

**三条路必须语义等价**——同一条 query 因为编译器选了不同宿主而结果不同,是这套设计最危险、最难查的一类 bug。`regexp_matches`(RE2)和 `grep -E`(POSIX ERE)在反向引用、lookahead、字符类上**并不一致**,所以多路算子的准入条件是 **differential test**(同一输入强制走每条路,断言逐行一致),不是可选测试。**把共用逻辑收进私有方法**(上面的 `_regex`)是控制这个风险最实际的手段——分歧只可能出现在你**故意**写得不同的地方。

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
> 唯一要作者声明的:**只有 bash 宿主的 source 要自报会不会结束**(`sh` 不报就按无界处理——`EXEC` 不可信)。其余算子什么都不用填。

**只借这两样(宿主降级的思路 + 有界性),别的不借。** **watermark / event time** ❌(seekbase 没有乱序事件流)、**checkpoint / exactly-once** ❌(单进程交互式查询,失败就重跑;持久性归写侧的 files-first + ticket,见 [store.md](store.md) / [ticket.md](ticket.md))、**keyed state / state backend** ❌(段间状态就是 `_in` 一张表;常驻资源是 §3.1 的服务句柄,不需要分区/快照/rescale)、**timer** ❌(用 `ctx.deadline`/`ctx.cancelled`)、**分布式 shuffle / 并行度** ❌(单进程;并行度是 **DuckDB 段内部**的事)、**窗口** 🕐 挂起(真出现无界 source 再说,届时按 SQL 的 window 语法接,不另造管道 DSL)。

> **澄清:as-of ≠ event time,`ds` ≠ watermark。** 这个类比很诱人但错。[time_machine.md](time_machine.md) 的 `ds` 是**行上的数据属性**,as-of 是一条**普通谓词**,下推进候选就完了;watermark 是「事件时间进展到哪」的**流控信号**,用来触发窗口、判迟到。把 `ds` 当 watermark 会平白引入迟到数据 / allowed lateness / side output 一整套**没有对应问题**的机械。
>
> **边界(和 pipeline §2.1 同一条老规矩):我们不造调度器。** DuckDB 段内部本来就是向量化推式执行(自己的 pipeline、morsel 并行、算子链),bash pipeline 本来就有内核背压。在它们之上再造一层算子调度,就是**重造一个更弱的 Flink**,和「把 `WHERE` 拆成 `where` 段」是同一类错误。算子只负责**告诉编译器自己在宿主里长什么样**,跑是宿主的事。

## 4. 格式与 coercion:`TABLE` 是缺省,跨边界才转

stage 之间流动的东西有**格式**。默认是 `TABLE`(一张关系,pipeline §2 的 ABI);为跨进程/跨算子还有几种编码。框架知道它们之间的 **coercion**,在**格式边界**自动插:

| 格式 | 是什么 | 谁用 |
|---|---|---|
| `TABLE` | 活关系(Arrow-backed / DuckDB 视图)——**缺省** | 进程内算子、SQL 段 |
| `ARROW` | Arrow IPC 字节(带类型) | 跨进程、快 |
| `JSONL` | 换行分隔 JSON(人可读) | 跨进程、`jq` 类 CLI |
| `BYTES` | 原始字节 / 文本流 | `sh` 等不透明算子 |
| `ROWS` | 物化行,交回调用方(终端) | sink |
| `NONE` | 空输入(unit) | source 的 `accepts` |

已知 coercion(框架自动):`TABLE ↔ ARROW ↔ JSONL`(互转,经 table)、`TABLE → ROWS`(物化)。`BYTES` **不自动**转 `TABLE`(不透明;要显式 parse 算子)。

- **同宿主同格式零成本**:两个都编译进 duck 的段之间根本没有序列化(它们是同一条 SQL 里相邻的 CTE)。
- **只有格式不同才 marshal**:`search`(emits=TABLE)`| jq`(accepts=JSONL)→ 框架在接缝插 `TABLE→JSONL` 喂 stdin、出来再 `JSONL→TABLE`。这就是**格式 coercion**,只在格式变的接缝发生。
- **`ExternalCommand` 作者不碰这个**:你声明 `accepts=JSONL, emits=JSONL`,coercion 框架管;你只写读 stdin JSONL、吐 stdout JSONL 的普通 CLI。
- **恒名 `_in`**:你不需要知道上一段是谁,只认 `_in`(duck 里它是上一个 CTE 名,bash 里是 stdin)。格式匹配(或可 coerce)就能接上,这让算子**可组合、可换位**。

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
| `ctx.deadline` / `ctx.cancelled` | 所有 | 超时/取消——长活算子要自觉检查 |

- **`ctx` 里有哪些 helper,由你的 `caps` 决定**:PURE 算子的 `ctx` 没有 `open_read`/`spawn`,想偷偷用也没有;`FS_READ` 算子的 `ctx.open_read` 被钉死在允许的根。**能力即接口**——声明多少、就只拿到多少。
- **`ctx` 只在 `start` 和 `run` 里出现**。`compile_duck`/`compile_bash` **拿不到 `ctx`**——它们只生成代码,不碰外界;真正的资源访问发生在宿主里,由沙箱和策略层管(operator-registry §6)。

## 6. 声明 caps:诚实是地基

`caps` 是权限系统的**唯一判据**(operator-registry §3/§6),所以**必须诚实**:

- **就低不就高**:纯表内运算声明 `PURE`,别顺手带 `FS_READ`。
- **按参数变 caps 就覆写 `parse_args`**:`grep <pat>` 表内 = `PURE`,`grep <pat> <path>` 读盘 = `FS_READ`——在 `parse_args` 里解析完再报给框架。**这是记录式写不出来的东西之一**(§2)。
- **声明不实 = 漏洞**:一个声明 `PURE` 却想联网的算子,`ctx` 里根本没有 `ctx.http`,调用即崩;真要联网就老实声明 `NET`,然后接受它默认受更严策略约束。
- **沙箱兜底**:`EXEC`/`FS_WRITE` 算子即使被策略放行,子进程仍在沙箱里(限目录、禁网、资源上限)——**框架不信你的声明,再加一道墙**(operator-registry §6.3)。

## 7. 输出 schema:下游 SQL 要知道你产了什么列

算子产的表接着被 SQL 段 `FROM _in` 查,所以**列名/类型要可知**。`out_schema` 是唯一的抽象方法(必须实现):

- **静态(推荐)**:`out_schema(in_schema, args) → schema` 在编译期就算出这一段后 `_in` 的 schema,下游 SQL 的列引用能**编译期校验**(引用不存在的列早失败)。`search` 的输出 = `hit_cols + (pk, _score:double)`。
- **late-bound(动态,慎用)**:像 `sh 'jq …'` 这种输出结构运行时才知道的,返回 `Schema.LATE`,由执行后从产物推断(`read_json_auto` 那套)。代价:下游 SQL 的列校验**推迟到运行期**,拼错列名要跑起来才炸。

> **静态 schema = 早失败 + 可优化**;late-bound = 灵活但把校验推到运行时。`sh` 的动态是逃生舱、不是常态。

## 8. 格式契约:`accepts` / `emits`(位置是推导的,不用 `kind`)

一个算子**不声明自己是 source/external/sink**——它只声明**能收哪些输入格式(`accepts`)、吐哪种输出格式(`emits`)**。它在管道里能放哪、算不算 source,全从格式**推导**:

| 你声明 | 推导出的角色 | 位置 | 例 |
|---|---|---|---|
| `accepts={NONE}`(= 继承 `Source`) | **source**(无上游) | 只能打头 | `search` `scan` `read` |
| `accepts={TABLE}`,`emits=TABLE` | 中间算子 | 中间 | `grep` `sed` `embed` |
| `accepts={JSONL}`,`emits=JSONL` | 中间算子(跨进程) | 中间 | `jq` `sh` |
| `emits=ROWS` | **sink**(终端输出) | 只能收尾 | `emit`(默认末端) |

- **`kind` 是多余的**:source = 「`accepts` 含 `NONE`」、sink = 「`emits` 是 `ROWS`」——都能从格式读出来,不必再手工贴一个可能和格式**打架**的标签(声明 `kind=source` 却 `accepts=TABLE` 就是自相矛盾;去掉 `kind`,这种矛盾根本不存在)。
- **同理,「覆写了什么」也是推导的,不是声明的**:有没有常驻状态看有没有覆写 `start`;能进哪个宿主看覆写了哪个 `compile_*`。**一律不设声明字段**——声明和实现能打架的地方,就是 bug 的产地。
- **格式匹配即可组合**:`A | B` 合法 ⟺ `emits(A) ∈ accepts(B)`,或存在已知 coercion(§4)。无路可转 → **编译期格式不匹配报错**。
- **多格式 = 更表达力**:一个算子可 `accepts={TABLE, JSONL}`,让框架挑最省的那条。**位置是一个维度,格式是一组**。
- **transform ≠ plugin**:一整条 DuckDB SQL 是管道缺省(pipeline §2.1),不进 registry、不用写类——首 token 不命中 registry 的段就是 SQL。

> **三个正交的轴,没有一个是声明字段**——各回答一个不同问题,全部从**覆写了什么**推导:
>
> | 轴 | 看什么 | 回答 | 决定 |
> |---|---|---|---|
> | **格式契约** | `accepts` / `emits` | 我能接谁? | 组合合法性 + coercion(§4/§8) |
> | **常驻状态** | 有没有覆写 `start` | 要不要复用贵资源? | 生命周期(§3.1) |
> | **宿主降级** | 覆写了哪几个 `compile_*` | 我能编译进哪个宿主? | 宿主指派 + 会不会成为切换点(§3.2) |
>
> ```
> Grep         = TABLE→TABLE   + 无状态      + compile: duck & bash   永不成为切换点
> LanceSearch  = NONE →TABLE   + 服务型      + compile: duck & bash   duck 走官方集成,bash 走 SDK
> Jq           = JSONL→JSONL   + 无状态      + compile: bash          在 duck 宿主里是切换点(架 vtab)
> 你的第一个算子 = TABLE→TABLE  + 无状态      + compile: 无            只覆写 run:能跑,最贵
> SQL 段        = TABLE→TABLE   + (非 plugin) + 天生 duck              整条链的锚点
> ```
>
> **最低门槛**:继承 `Operator`,实现 `out_schema` + `run`,收工。`start`/`stop` 和 `compile_*` 都是**为了跑得便宜才覆写的第二档**。

## 9. 注册:挂进 registry

```python
db = await Seekbase.open("./data", schema=SCHEMA, operators=[
    Grep,                              # 传类:框架 cls() 实例化,再 await start()
    LanceSearch(uri="./data/lance"),   # 传实例:需要构造参数时自己配好
    Jq,
])
```

- **类和实例都收**:无构造参数的传类(框架实例化),要配置的传实例。框架无论哪种都会 `await start()` / `await stop()`。
- **名字规则**:不取 SQL 引导关键字(`select`/`with`/`from`…),否则会和「SQL 缺省」相撞(operator-registry §5);和内建/已注册**同名 → 显式报错**,不覆盖。
- **子类不自动注册**:定义一个 `Operator` 子类不等于注册它;必须显式列进 `operators=`。(隐式注册 = 导入一个模块就多个算子,权限面失控。)
- **内建 + 用户注册同一张表**:你的算子和 `search` 平权;用户算子**必须声明 caps**,进不了「审过」名单、默认按声明 caps 受策略约束 + 沙箱。

## 10. 三个完整例子(从最简到最复杂)

### 10.1 `Grep`(最简:无状态,直接继承 `Operator`)

```python
class Grep(Operator):
    name    = "grep"
    accepts = {Fmt.TABLE}
    emits   = Fmt.TABLE

    class Args(ArgSpec):
        pattern: str
        field:   str | None = None
        path:    str | None = None                       # 给了 path 就是读盘

    def parse_args(self, tokens):                        # ★ 覆写:按参数决定 caps(§6)
        a = super().parse_args(tokens)
        self.caps = {Cap.FS_READ} if a.path else {Cap.PURE}
        return a

    def out_schema(self, in_schema, args):
        return in_schema                                 # 只过滤行,列不变

    def _regex(self, args):                              # ★ 三条路共用(私有方法)
        return args.pattern

    def compile_duck(self, prev, args):
        return (f"SELECT * FROM {prev} "
                f"WHERE regexp_matches({args.field or '*'}, {self._regex(args)!r})")

    def compile_bash(self, args):
        return ["grep", "-E", self._regex(args)]

    def run(self, in_table, args, ctx):                  # 保底(被包成 vtab)
        pat = re.compile(self._regex(args))
        return in_table.filter(lambda r: pat.search(str(r[args.field])))
```

用:`search cards '事故' | grep 'ERROR' --field issue | SELECT card_id FROM _in LIMIT 20`。**没有常驻状态**——没覆写 `start`/`stop`,关库时无事可做。

### 10.2 `Search`(最复杂:服务型 + 一父两子)—— 继承在这里才真正兑现

`search` 复杂**不在算法**(RRF 就那样),而在两点:① 它是个**服务**(常驻引擎 + 载进 RAM 的 HNSW + embedder 池,开一次复用);② 它有**两个可插拔后端**(LanceDB / DuckDB-vss,见 [search.md](search.md))。这正好是「**一个父类抽公共、两个子类各自降级**」:

```python
class Search(Source):                                     # 父类:accepts={NONE} 由 Source 预设
    name = "search"
    caps = {Cap.PURE}                                     # 引擎在库内;embedder 的 NET 由 ctx 中介

    class Args(ArgSpec):
        table: str
        text:  str
        k:     int = 10

    def out_schema(self, _in, args):                      # ★ 公共:两个后端产同样的列
        return Schema([("pk", "str"), ("_score", "double")]) + hit_cols(args.table)

    def _query_vec(self, args, ctx):                      # ★ 公共:embed + 分词
        return self.embed(args.text), ctx.tokenize(args.text)


class LanceSearch(Search):                                # 后端 A:LanceDB
    async def start(self, ctx):                           # ★ open 时一次:拉起常驻服务(重活)
        self.engine = await ctx.open_engine("lance")      #   开连接 + 把 HNSW 载进 RAM
        self.embed  = ctx.embedder                        #   常驻 embedder 客户端

    def compile_bash(self, args):                        # bash 宿主:小命令直调 LanceDB SDK
        return ["seekbase-search", args.table, args.text, "--k", str(args.k)]

    def compile_duck(self, prev, args):                  # duck 宿主:DuckDB×LanceDB 官方集成
        return (f"SELECT * FROM lance_search({args.table!r}, {args.text!r}, "
                f"k := {args.k}, asof := current_asof())")

    def run(self, _in, args, ctx):                        # 保底:进程内直查,复用 self.engine
        qvec, qtok = self._query_vec(args, ctx)           # ★ 绝不在这里 open_engine
        return self.engine.hybrid(args.table, qvec, qtok, k=args.k, asof=ctx.asof)

    async def stop(self):                                 # ★ close 时一次:拆常驻服务
        await self.engine.close()


class VssSearch(Search):                                  # 后端 B:DuckDB-vss + fts,同一父类
    async def start(self, ctx):
        self.engine = await ctx.open_engine("vss")
        self.embed  = ctx.embedder

    def compile_duck(self, prev, args):                  # 本来就在 duck 里,最自然
        return f"SELECT * FROM vss_hybrid({args.table!r}, {args.text!r}, k := {args.k})"

    def run(self, _in, args, ctx):
        qvec, qtok = self._query_vec(args, ctx)
        return self.engine.hybrid(args.table, qvec, qtok, k=args.k, asof=ctx.asof)

    async def stop(self):
        await self.engine.close()
```

用:`search cards 'pty 终端' | SELECT * FROM _in WHERE kind='issue' ORDER BY _score DESC LIMIT 20`。换后端 = 注册 `LanceSearch()` 还是 `VssSearch()`,管道一个字不改。

**这一节的重点是对照**:

| | `Grep`(无状态) | `Search`(服务型) |
|---|---|---|
| 常驻资源 | 无 | 引擎连接 + RAM 里的 HNSW + embedder 池 |
| 覆写的钩子 | `out_schema` + 降级 | 再加 `start` / `stop` |
| 每次调用成本 | 就地算,自给自足 | 复用 `self.engine`;**重活在 `start`,不在 `run`** |
| 关库 | 无事可做 | 必须 `stop` 释放索引内存 / 关连接 |
| 多实现 | 一个类够了 | 一父两子:公共逻辑在父类,降级各写各的 |

> 关键不变量:**`run` 里绝不 `open_engine`**——那是 `start` 的活。把「开一次的贵资源」错放进 `run`,就是每次查询都重载一遍索引,服务型算子的意义全丢。
>
> 这个一父两子的形状,就是**为什么 plugin 必须是类**:公共的 `out_schema` / `_query_vec` 写一遍,两个后端只写各自不同的降级。记录式在这里只能复制两份,然后它们慢慢长歪。

### 10.3 `Jq`(包一个 CLI:继承 `ExternalCommand`,几乎不用写代码)

```python
class Jq(ExternalCommand):
    name    = "jq"
    argv    = ["jq", "-c", "{script}"]                   # 基类据此替你实现 compile_bash
    accepts = {Fmt.JSONL}                                # jq 认 JSONL 进、JSONL 出
    emits   = Fmt.JSONL                                  # 框架自动 coerce 上下游 TABLE↔JSONL(§4)

    class Args(ArgSpec):
        script: str

    def out_schema(self, in_schema, args):
        return Schema.LATE                               # jq 的输出结构运行时才知道(§7)
```

用:`search cards '事故' | jq 'select(.severity>=3)' | SELECT kind, count(*) FROM _in GROUP BY kind`。默认 `read-only` 策略下 **`jq` 因 `EXEC` 被拒**(`ExternalCommand` 预设了 `caps={Cap.EXEC}`),要显式升级到 `sandboxed`/`trusted` 才能跑(operator-registry §6)。**只有 `compile_bash`** ⇒ 它在 duck 宿主里是个切换点,编译器会为它架一座 vtab 桥。

## 11. 生命周期:register → start → parse → authorize → compile → run → stop

```
open(operators=[…])          实例化 + 注册(name 不撞、caps 记下);await start(ctx) 拉起常驻服务
   │
parse 管道               每段首 token 命中 registry?→ 是=算子 / 否=SQL 缺省;parse_args 校验参数
   │
authorize                算子 caps vs 策略 → allow / ask / deny(deny→编译期拒,管道不启动)
   │
compile                  宿主指派 → 调 compile_duck / compile_bash 生成代码;都没有就包 vtab 调 run
   │
execute                  宿主跑:一条 DuckDB SQL 和/或一条 bash pipeline —— 不是我们跑
   │
close                    await stop():关连接、释放索引内存;无状态算子无事可做
```

作者只管覆写点;实例化 / 授权 / 宿主指派 / 沙箱 / 生命周期编排都是框架的活——它在 `open` 时替你 `start`、`close` 时 `stop`。

## 12. 测试一个算子

- **单测 `run`**:喂一张构造的 Arrow 表 + 一个 fake `ctx`(按 caps 只放对应 helper),断言输出 schema + 行。
- **多路 differential test(准入条件,非可选)**:同一份输入,分别**强制**走 `compile_duck` / `compile_bash` / `run`,断言逐行一致。三条路结果不同 = 同一条 query 换宿主换答案,这是本设计最危险的 bug(§3.2)。**父类可以直接提供这个测试基类**,子类只填输入——这是继承的又一处兑现。
- **服务型测生命周期**:`start` 后断言 handle 建好、多次调用**复用**同一 handle(mock 引擎断言 `open` 只调一次)、`stop` 后释放。要有实例才测得了。
- **一父多子测公共契约**:同一套断言跑在 `LanceSearch` 和 `VssSearch` 上,保证两个后端行为一致(§10.2)。
- **无界测编译期**:bash 宿主的无界 source 接一条 SQL 段,断言**编译期**报错(而不是跑起来挂住——这条测试的全部意义就是防它退化回挂死,§3.3)。
- **降级测 `EXPLAIN`**:断言宿主指派符合预期(比如「加了 `compile_duck` 之后切换点从 2 变 0」),否则性能回退无人察觉。
- **caps 负测**:给 PURE 算子的 fake `ctx` 不放 `open_read`,断言它没偷偷读盘(调了就 `CapabilityViolation`)。
- **策略测**:`read-only` 下 `EXEC`/`NET` 算子被 deny;升级后放行且仍在沙箱内。

## 13. 诚实的代价 / 边界

- **继承的老账**:基类一改,所有子类跟着动;`Operator` 的方法签名等于对外 API,轻易改不得。换来的是 §2 那五件记录式做不到的事——**只有一层继承**是控制这笔账的方式(§2)。
- **多路实现 = 多份维护 + 等价性风险**(§3.2)。只覆写一个 compile_* 是完全正当的选择,它只意味着这个算子**是个切换点**——请在文档里写明,别让用户猜为什么某条管道突然变慢。
- **只覆写 `run` 不是错,是贵**:保底 vtab 一定跑得通,但它是**优化屏障**(DuckDB 看不穿桥、谓词推不进去、基数估计失真)+ 每批 marshal。热路径上的算子值得补一个 compile_*。
- **服务型 = 常驻内存 + 生命周期负担**:`search` 的引擎连接 + HNSW 索引常驻 RAM(天花板见 [search.md §5](search.md)),绑在 `open`/`close` 上——起得慢、占内存、忘了 `stop` 会漏。**够用就别做成服务型**。
- **输出 schema 是契约**:一旦有下游 SQL 依赖你的列,改列名/类型 = 破坏性变更。late-bound 更脆——所以尽量静态。
- **`run` 阻塞事件循环**:它是同步的,重活要走 bridge/线程(和 DuckDB 一样,见 [concurrency.md](concurrency.md))。`start`/`stop` 是 async(开/关引擎是 io)。
- **今天 seekbase 没有一个无界 source**。所以 §3.3 那条检查现在的收益只有:`sh 'tail -f'` 从「挂死」变成编译期报错。值不值取决于你信不信「管道能串 shell ⇒ 迟早有人串 `tail -f`」——我认为会,而**挂死是最贵的一类故障**,且这条检查几乎不要钱(只问宿主,不做传播)。
- **可解释性变差**:用户写 5 段管道,跑的是 1 条 SQL + 1 个子进程。报错行号、性能归因都要能映射回用户写的那一段——所以 `EXPLAIN` 是必需品,不是奢侈品。
- **不做 exactly-once**:管道中途失败 = 整条重跑。读路径无副作用所以安全;但 `FS_WRITE`/`NET` 算子**重跑就是重复副作用**——幂等性归作者,框架不担保。
- **caps 可信度**:整套授权建立在「作者诚实声明 caps」上;沙箱兜 `EXEC`/`FS_WRITE`,但弱平台上沙箱退化,此时靠策略 `deny` 名单硬关(operator-registry §8)。
- **不管数据权限**:算子契约管「算子能碰哪些资源」,**不**管「这次调用能看表 T 的哪些行」(行级授权是另一层)。

## 14. 与其他文档

- [operator-registry.md](operator-registry.md):系统视角——registry、caps 分级、能力×策略权限、沙箱(本文是它的作者侧)。
- [pipeline-as-anything.md](pipeline-as-anything.md):`_in` 表 ABI、SQL 缺省、§2.1「接缝才切」。
- [pipeline-runtime-optimize.md](pipeline-runtime-optimize.md):**本文 `compile_*` 的去处**——宿主指派、融合切段、vtab 桥、代价阶梯。作者视角看「覆写几个 compile_*」,系统视角看「编译器怎么用它们省钱」。
- [search.md](search.md):`Search` 一父两子就是那篇的**可插拔引擎**(LanceDB / DuckDB-vss)在算子层的落法(§10.2)。
- [time_machine.md](time_machine.md):`ctx.asof` 语义,source 如何下推可见性;**`ds` 不是 watermark**(§3.3 澄清)。
- [concurrency.md](concurrency.md):`run` 为什么重活要下沉到 bridge/线程(保底 vtab 的批回调同理)。
