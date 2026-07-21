# operator-plugin — 写一个算子:`Operator` 基类的契约与实现

> 状态:**部分已落**(`seekbase/operator/base.py`:`Operator` 基类 + `optimize_duck` 原生降级 + prepare 钩子 + 签名推导 position;`run_duck`/`run_bash`/`optimize_bash` 是留位的契约面,M2)。[operator-registry.md](operator-registry.md) 讲**系统视角**(registry 怎么存、权限怎么判);本文讲**作者视角**:你要给 seekbase 加一个管道算子(`search` 是内建的一个、`grep`/`find`/`sh` 是另几个),得实现一个什么样的 **plugin**?
>
> **一个 plugin = 一个算子(operator)= 一个 `Operator` 子类。** 管道就是一串算子,框架只定**算子基类**,谁都能继承它插进来——`search`、`grep`、`jq` 在基类面前是同一种东西。
>
> **但 seekbase 不自己跑管道**:整条管道被**降级**到一个 runtime(DuckDB `WITH` / bash pipeline,[pipeline-runtime-optimize.md](pipeline-runtime-optimize.md))。所以算子的方法**按 runtime 分**,分两轴:
> - **物化执行(`run_*`,必修基线之一)**:`run_duck(in_table)→table`(duck 里当 vtab 物化处理)、`run_bash(stdin,stdout)`(bash 里当一个进程、自己 `ctx.spawn` 起子进程处理)。有屏障 + 每批 marshal,但**真干活**。
> - **原生降级(`optimize_*`,选修加速)**:`optimize_duck→SQL`、`optimize_bash→argv`,编译期产代码、**0 成本、无屏障**,但只能表达成一条 SQL / 一条命令的才写得出。
>
> 四个方法**全可选、≥1 非空**;有 `optimize_R` 就走原生,没有就退 `run_R`(物化)。**没有 `accepts`/`emits`**——格式是 runtime 的介质(duck=table、bash=字节流),由你在哪一格定死,不用声明(§4/§8)。
>
> 依赖:[operator-registry.md](operator-registry.md)(registry、caps 分级、能力×策略权限)、[pipeline-as-anything.md](pipeline-as-anything.md)(`_in` 表 ABI、SQL 是缺省)。

## 1. 定位:一个算子 = 一个 `Operator` 子类

管道里每段非-SQL 的 verb 都由一个 plugin 支撑。**框架不认识 `search` 也不认识 `grep`,它只认识 `Operator`**——`search` 和你自己写的算子继承的是同一个基类(这就是「可插拔算子机制」的全部意思)。一个 plugin 就是**一个子类**:

```python
class Grep(Operator):
    name = "grep"                             # 无 accepts/emits——格式是 runtime 介质(§4/§8)
    caps = {Cap.PURE}                         # 诚实声明碰什么外界资源(§6)

    class Args(ArgSpec):                      # 参数签名:供解析 + --help
        pattern: str
        field:   str | None = None

    def out_schema(self, in_schema, args):    # 回到 duck 时下游 SQL 要知道的列(§7)
        return in_schema                      #   只过滤行,列不变

    def optimize_duck(self, prev, args):      # 原生:一段 SQL,0 成本(§3.2)
        return f"SELECT * FROM {prev} WHERE {self._sql_pred(args)}"

    def optimize_bash(self, args):            # 原生:一条命令
        return ["grep", "-E", args.pattern]
```

**声明放类属性,行为放方法覆写。** 该覆写哪些、可以不覆写哪些,见 §2。`grep` 两个 runtime 都能原生降级(两个 `optimize_*`),所以连物化的 `run_*` 都不用写。

> **不用 `kind`,也不用 `accepts`/`emits`**:算子不声明自己是 source/external/sink,也不声明格式——是不是 source(不吃上游)由 `run_*`/`optimize_*` 的**签名**推导,格式由**落在哪个 runtime**定死(§8)。分类和格式都是投影,不是要维护的字段。

## 2. 一个基类,几处覆写点

```python
class Operator(ABC):
    # ── 声明:类属性(无 accepts/emits,格式是 runtime 介质,§4)──
    name: ClassVar[str]
    caps: ClassVar[set[Cap]] = {Cap.PURE}
    class Args(ArgSpec): ...                          # 内嵌参数签名

    def parse_args(self, tokens) -> Args: ...         # 可覆写:自定义校验 / 按参数推导 caps(§6)

    # ── schema:必须实现(回到 duck 时下游 SQL 要知道的列,§7)──
    @abstractmethod
    def out_schema(self, in_schema, args) -> Schema: ...

    # ── 物化执行(run_*):有屏障 + ctx,真干活;≥1 个非空(§3.2)──
    def run_duck(self, in_table, args, ctx): raise NotSupported   # table→table,当 vtab
    def run_bash(self, stdin, stdout, args, ctx): raise NotSupported  # 进程,ctx.spawn 起子进程

    # ── 原生降级(optimize_*):0 成本、无 ctx;可选加速(§3.2)──
    def optimize_duck(self, prev, args) -> str: raise NotSupported     # 一段 SQL
    def optimize_bash(self, args) -> list[str]: raise NotSupported     # 一条 argv

    # ── 生命周期:服务型才覆写(§3.1)────────────────────────
    async def start(self, ctx) -> None: pass
    async def stop(self) -> None:       pass
```

四个执行方法**全可选,但≥1 非空**(否则这算子没法跑)。任一个都能被桥进另一个 runtime(带 relation↔字节 coercion,§4),所以「必修」不是某个具体方法,而是「四选一有实现」。加一个**只预设默认值**的薄子类,不是新概念:

| 基类 | 预设了什么 | 你还要写什么 |
|---|---|---|
| `Operator` | 什么都不预设 | 全部 |
| `ExternalCommand(Operator)` | `caps = {Cap.EXEC}`,并**替你实现 `optimize_bash`**(按 `argv` 模板渲染) | 只填 `argv` |

> **层次是浅的,只有一层。** 不搞 `AbstractBaseFilterOperatorFactory` 那套;`ExternalCommand` 就是省几行样板,你随时可以直接继承 `Operator` 自己写。source(不吃上游)不需要专门基类——它只是 `run_*`/`optimize_*` 的签名里**不吃 `in`/`prev`**,由签名推导(§8)。

**为什么是继承,而不是「填一条记录 + 几个 lambda」**——记录式撑不住稍微复杂一点的算子:

- **状态没处放**。服务型算子要在 `start` 里开引擎、把 handle 留到每次调用复用(§3.1)。没有 `self`,handle 只能塞模块级全局——多实例(两个后端、两个库)直接崩。
- **覆写点之间无法共享逻辑**。`optimize_duck`、`optimize_bash`、`run_duck` 几条路要产生**同一个语义**(§3.2),它们必然共用参数校验、谓词构造、列名推导。几个独立函数只能靠模块级 helper 或复制粘贴;有 `self` 就是一个私有方法。
- **不能复用**。`search` 的 lance 后端和 vss 后端 90% 相同(参数、schema、RRF 语义),差的只是降级实现——那天然是**一个父类 + 两个子类**(§10.2)。记录式只能复制两份,然后它们慢慢长歪。
- **不能按参数变行为**。`grep <pat>` 是 `PURE`、`grep <pat> <path>` 是 `FS_READ`(§6);这要覆写 `parse_args`,记录里无处覆写。
- **没法测**。服务型的 `start`/`run`/`stop` 要断言「handle 只开一次」,得有实例才能测(§12)。

## 3. 核心契约

物化 `run_duck`/`run_bash`(有屏障)+ 原生 `optimize_duck`/`optimize_bash`(0 成本),**同一算子的这几条路语义必须一致**;加上一对生命周期钩子。逐条看:

### 3.1 无状态 vs 服务型:要不要常驻进程

按**背后有没有常驻状态**分两类——**这正是 `grep` 简单、`search` 复杂的根源**:

- **无状态算子(如 `grep`)**:**没有常驻进程**,每次调用自给自足,调用完什么都不留。`start`/`stop` **不覆写**(基类的空实现就对),框架看到没覆写就当它零常驻。
- **服务型算子(如 `search`)**:背后是一个**常驻服务**——向量引擎连接、**载进 RAM 的 HNSW 索引**、embedder 连接池。这些**开一次、复用多次**,**绝不能每次调用都重开**:

  | 钩子 | 何时 | 干什么 |
  |---|---|---|
  | `start(ctx)` | `open` 时一次 | 拉起常驻服务:开引擎、载索引进 RAM、暖 embedder;**存进 `self`** |
  | `run_*` / `optimize_*` | 每次调用 | **复用 `self` 上的 handle**,绝不重开 |
  | `stop()` | `close` 时一次 | 拆:关连接、释放索引内存 |

> 分界线:**「每次调用要不要复用一份贵的、开一次的资源」**。要 → 覆写 `start`/`stop`;不要 → 别覆写。`search` 的引擎 + RAM 常驻索引就是那份贵资源,`grep` 什么都不用留。
>
> **这也是必须用类的最直接原因**:handle 存 `self`,天然支持一个进程里跑多个实例(两个后端 / 两个库),而模块级全局做不到。

### 3.2 两轴四方法:物化 `run_*` × 原生 `optimize_*`

管道不由 seekbase 执行,而是被编译成 **DuckDB `WITH` 链**或 **bash pipeline**([pipeline-runtime-optimize.md](pipeline-runtime-optimize.md))。所以每个算子的执行方法按**两根轴**排成四格:

|  | **duck runtime**(介质 = 关系) | **bash runtime**(介质 = 字节流) |
|---|---|---|
| **原生 `optimize_*`**(0 成本、无 `ctx`) | `optimize_duck(prev, args) → SQL`:算子**变成一段 SQL**融进 `WITH` 链 | `optimize_bash(args) → argv`:算子**变成一条命令**融进 shell 管道 |
| **物化 `run_*`**(有屏障、有 `ctx`) | `run_duck(in_table, args, ctx) → table`:一个 **vtab**,在 Python 里物化处理 | `run_bash(stdin, stdout, args, ctx)`:管道里一个**进程**,自己 `ctx.spawn` 起子进程处理 |

- **`optimize_*` 是编译期产代码**(SQL 文本 / argv,**不碰数据、拿不到 `ctx`**),让这段**原生融进** runtime——零 Python、无屏障、优化器/内核直接接管。能表达成**一条 SQL / 一条命令**的才写得出。
- **`run_*` 是运行期真干活**(物化):`run_duck` 是 DuckDB 表函数(vtab)回调,`run_bash` 是 shell 管道里的一个进程、通常在体内 `ctx.spawn` 拉起子进程去处理。**物化就意味着屏障 + 每批 marshal**——**这正是 `optimize_*` 存在的理由**:能原生就别物化。

```python
class Grep(Operator):
    def _regex(self, args):                    # ★ 几条路共用一份参数逻辑(记录式做不到)
        return args.pattern

    def optimize_duck(self, prev, args):        # 原生 → 一段 SQL(翻成 WHERE)
        col = args.field or "*"
        return f"SELECT * FROM {prev} WHERE regexp_matches({col}, {self._regex(args)!r})"

    def optimize_bash(self, args):              # 原生 → 一条 argv(就是 grep 本身)
        return ["grep", "-E", self._regex(args)]
    # grep 两个 runtime 都能原生 ⇒ 连 run_* 都不用写
```

> **`optimize_duck` / `optimize_bash` / `run_duck` / `run_bash` 里的 duck/bash 只是当下两个 runtime —— 方法名里嵌的是 runtime 名,而 runtime 是开放集。** 加一个 runtime `R` 就是多两个可覆写的 `optimize_R` / `run_R`,基类不动、老算子不改。别把 runtime 和 `search` 内部的**引擎后端**(LanceDB / duck-vss)混为一谈——那是两层(pipeline-runtime-optimize §1.1)。

**覆写哪几格决定你贵不贵**——由框架**检测覆写**得出(不用声明,和 `kind` 同一原则,§8):

| 落进某 runtime 的一段 | 用哪格 | 成本 |
|---|---|---|
| 有该 runtime 的 `optimize_*` | 原生融入 | **0** |
| 只有该 runtime 的 `run_*` | 物化(vtab / 进程) | 屏障 + 每批 marshal |
| 只有**另一** runtime 的实现 | 桥过去 + relation↔字节 coercion | 屏障 + marshal + 一次转码 |

> **写 `optimize_*` 的理由不是「能在两边跑」,是「不成为切换点、不物化」。** 一堆 duck 段中间夹一个只会 bash 的 `grep`,代价是整条管道劈开 + 一次物化;给它一个 `optimize_duck`(把 grep 整个翻译成 `WHERE`),代价直接归零。收益是复利的,见 [pipeline-runtime-optimize.md §5](pipeline-runtime-optimize.md)。

**同一算子的几格必须语义等价**——同一条 query 因为编译器选了不同 runtime / 不同格而结果不同,是这套设计最危险、最难查的一类 bug。`regexp_matches`(RE2)和 `grep -E`(POSIX ERE)在反向引用、lookahead、字符类上**并不一致**,所以多格算子的准入条件是 **differential test**(同一输入强制走每一格,断言逐行一致),不是可选测试。**把共用逻辑收进私有方法**(上面的 `_regex`)是控制这个风险最实际的手段——分歧只可能出现在你**故意**写得不同的地方。

### 3.3 有界性:一条规则,不是一套机制

> **无界流 / `tail -f` / 流式语义在 [pipeline-streaming.md](pipeline-streaming.md) 细谈**(bash 管道当简易流框架),这里只钉最小的一条编译期规则,不展开。

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

## 4. 格式是 runtime 的介质,不是你声明的

**没有 `accepts`/`emits`。** 一段流动的东西是什么格式,由它**落在哪个 runtime** 定死:

| runtime | 介质 | 你在这一格写的方法收/返什么 |
|---|---|---|
| duck | **关系(table)** | `optimize_duck` 产 SQL、`run_duck` 收/返 Arrow 表 |
| bash | **字节流(stdin/stdout)** | `optimize_bash` 产命令、`run_bash` 读/写 stdin/stdout |

- **同 runtime 相邻段零成本**:两个都落在 duck 就是同一条 SQL 里相邻的 CTE,没有序列化;两个都落在 bash 就是内核管道的 `|`,内核给背压。
- **coercion 只在 duck↔bash 边界发生,且是框架的活**:跨 runtime 时框架把**关系 ⇄ 字节流**转一次(Arrow IPC 优先,JSONL 退化)。算子作者**不碰这个**——你只在自己那一格的介质上写代码。
- **「格式」退成实现细节**:`jq` 从 stdin 读的是 JSONL?那是 `optimize_bash`/`run_bash` 内部怎么解析字节流的事,不是对外契约。老设计里 `accepts={JSONL}` 那种声明**没了**——bash 段的介质就是字节流,里面装 JSONL 还是别的由算子自己认。
- **恒名 `_in`**:你不需要知道上一段是谁,只认 `_in`(duck 里是上一个 CTE 名,bash 里是 stdin)。这让算子**可组合、可换位**;能不能接上不再靠格式匹配,而是「同 runtime 直接接 / 跨 runtime 框架转一次」——**永远接得上**。

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
- **`ctx` 只在 `start` 和 `run_duck` / `run_bash` 里出现**。`optimize_duck` / `optimize_bash` **拿不到 `ctx`**——它们只生成代码,不碰外界;真正的资源访问发生在生成的 SQL/命令跑起来时,由沙箱和策略层管(operator-registry §6)。`run_bash` 起子进程就走 `ctx.spawn`(吃 `EXEC`、进沙箱)——**声明与执行天然对齐**。

## 6. 声明 caps:诚实是地基

`caps` 是权限系统的**唯一判据**(operator-registry §3/§6),所以**必须诚实**:

- **就低不就高**:纯表内运算声明 `PURE`,别顺手带 `FS_READ`。
- **按参数变 caps 就覆写 `parse_args`**:`grep <pat>` 表内 = `PURE`,`grep <pat> <path>` 读盘 = `FS_READ`——在 `parse_args` 里解析完再报给框架。**这是记录式写不出来的东西之一**(§2)。
- **声明不实 = 漏洞**:一个声明 `PURE` 却想联网的算子,`ctx` 里根本没有 `ctx.http`,调用即崩;真要联网就老实声明 `NET`,然后接受它默认受更严策略约束。
- **沙箱兜底**:`EXEC`/`FS_WRITE` 算子即使被策略放行,子进程仍在沙箱里(限目录、禁网、资源上限)——**框架不信你的声明,再加一道墙**(operator-registry §6.3)。

## 7. 输出 schema:下游 SQL 要知道你产了什么列

当一段的产物回到 duck、被 SQL 段 `FROM _in` 查时,**列名/类型要可知**(bash 段之间不需要,字节流没有列;一旦要重进 duck 就需要)。`out_schema` 是唯一的抽象方法(必须实现):

- **静态(推荐)**:`out_schema(in_schema, args) → schema` 在编译期就算出这一段后 `_in` 的 schema,下游 SQL 的列引用能**编译期校验**(引用不存在的列早失败)。`search` 的输出 = `hit_cols + (pk, _score:double)`。
- **late-bound(动态,慎用)**:像 `sh 'jq …'` 这种输出结构运行时才知道的,返回 `Schema.LATE`,由执行后从产物推断(`read_json_auto` 那套)。代价:下游 SQL 的列校验**推迟到运行期**,拼错列名要跑起来才炸。

> **静态 schema = 早失败 + 可优化**;late-bound = 灵活但把校验推到运行时。`sh` 的动态是逃生舱、不是常态。

## 8. 位置从签名推导,不用 `kind`、不用 `accepts`/`emits`

一个算子**不声明自己是 source/external/sink,也不声明格式**。它在管道里能放哪,全从**它的方法吃不吃上游**推导:

| 方法签名 | 推导出的角色 | 位置 | 例 |
|---|---|---|---|
| `run_*`/`optimize_*` **不吃 `in`/`prev`** | **source**(无上游) | 只能打头 | `search` `scan` `read` |
| 吃上游、产下游 | 中间算子 | 中间 | `grep` `sed` `jq` |
| 产终端结果、无下游 | **sink** | 只能收尾 | `emit`(默认末端) |

- **`kind` 和 `accepts`/`emits` 都是多余的**:source = 「方法不吃上游」——从**签名**读得出,不必再贴一个可能和实现**打架**的标签或格式声明(声明 `kind=source` 却写了吃 `prev` 的 `optimize_duck` 就是自相矛盾;不声明,这种矛盾根本不存在)。
- **一切都从「覆写了什么 + 签名长什么样」推导,一律不设声明字段**:是不是 source 看签名吃不吃上游;有没有常驻状态看有没有覆写 `start`;能进哪个 runtime、原生还是物化看覆写了四格里的哪几格。**声明和实现能打架的地方,就是 bug 的产地。**
- **组合永远合法**:`A | B` 不再靠格式匹配——同 runtime 直接接,跨 runtime 框架转一次(§4)。没有「格式不匹配」这种编译期错误了。
- **transform ≠ plugin**:一整条 DuckDB SQL 是管道缺省(pipeline §2.1),不进 registry、不用写类——首 token 不命中 registry 的段就是 SQL。

> **两个正交的轴,没有一个是声明字段**——各回答一个不同问题,全部从**覆写了什么 + 签名**推导:
>
> | 轴 | 看什么 | 回答 | 决定 |
> |---|---|---|---|
> | **执行矩阵** | 覆写了四格(`{optimize,run}×{duck,bash}`)里哪几格 | 我能在哪个 runtime 跑、原生还是物化? | runtime 指派 + 成本(§3.2) |
> | **常驻状态** | 有没有覆写 `start` | 要不要复用贵资源? | 生命周期(§3.1) |
>
> (格式不是轴——它是 runtime 的介质,§4;position 不是轴——它从签名推,上表。)
>
> ```
> Grep         = optimize_duck + optimize_bash             + 无状态   两 runtime 都原生,永不物化
> LanceSearch  = optimize_duck + optimize_bash + run_duck  + 服务型   source(不吃上游);两原生 + 一物化兜底
> Jq           = optimize_bash                             + 无状态   只 bash 原生;进 duck 靠桥
> 你的第一个算子 = run_duck                                  + 无状态   只物化;哪都能跑,最贵
> SQL 段        = (非 plugin,天生 duck)                                整条链的锚点
> ```
>
> **最低门槛**:继承 `Operator`,实现 `out_schema` + 四格里**任一格**,收工。`start`/`stop` 和多写几格都是**为了跑得便宜 / 跑得到更多 runtime 才覆写的第二档**。

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

### 10.1 `Grep`(最简:两 runtime 都原生,连 `run_*` 都不用写)

```python
class Grep(Operator):
    name = "grep"                                        # 无 accepts/emits(§4)

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

    def _regex(self, args):                              # ★ 两格共用(私有方法)
        return args.pattern

    def optimize_duck(self, prev, args):                 # 原生 SQL
        return (f"SELECT * FROM {prev} "
                f"WHERE regexp_matches({args.field or '*'}, {self._regex(args)!r})")

    def optimize_bash(self, args):                       # 原生命令
        return ["grep", "-E", self._regex(args)]
```

用:`search cards '事故' | grep 'ERROR' --field issue | SELECT card_id FROM _in LIMIT 20`。**两个 runtime 都原生降级**,所以永远 0 成本、永不成为切换点,连物化的 `run_*` 都不用写;也没有常驻状态。

### 10.2 `Search`(最复杂:服务型 + 一父两子)—— 继承在这里才真正兑现

`search` 复杂**不在算法**(RRF 就那样),而在两点:① 它是个**服务**(常驻引擎 + 载进 RAM 的 HNSW + embedder 池,开一次复用);② 它有**两个可插拔后端**(LanceDB / DuckDB-vss,见 [search.md](search.md))。这正好是「**一个父类抽公共、两个子类各自降级**」:

```python
class Search(Operator):                                   # source:下面的方法都不吃上游 ⇒ 推导为 source(§8)
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

    def optimize_bash(self, args):                        # 原生:小命令直调 LanceDB SDK(不吃 prev ⇒ source)
        return ["seekbase-search", args.table, args.text, "--k", str(args.k)]

    def optimize_duck(self, args):                        # 原生:DuckDB×LanceDB 官方集成
        return (f"SELECT * FROM lance_search({args.table!r}, {args.text!r}, "
                f"k := {args.k}, asof := current_asof())")

    def run_duck(self, _in, args, ctx):                   # 物化兜底:进程内直查,复用 self.engine
        qvec, qtok = self._query_vec(args, ctx)           # ★ 绝不在这里 open_engine
        return self.engine.hybrid(args.table, qvec, qtok, k=args.k, asof=ctx.asof)

    async def stop(self):                                 # ★ close 时一次:拆常驻服务
        await self.engine.close()


class VssSearch(Search):                                  # 后端 B:DuckDB-vss + fts,同一父类
    async def start(self, ctx):
        self.engine = await ctx.open_engine("vss")
        self.embed  = ctx.embedder

    def optimize_duck(self, args):                        # 本来就在 duck 里,最自然
        return f"SELECT * FROM vss_hybrid({args.table!r}, {args.text!r}, k := {args.k})"

    def run_duck(self, _in, args, ctx):
        qvec, qtok = self._query_vec(args, ctx)
        return self.engine.hybrid(args.table, qvec, qtok, k=args.k, asof=ctx.asof)

    async def stop(self):
        await self.engine.close()
```

(`optimize_duck` 作为 source 不吃 `prev` —— 中间算子的 `optimize_duck(self, prev, args)` 才吃;source 少那个参数,§8 据此推导。)用:`search cards 'pty 终端' | SELECT * FROM _in WHERE kind='issue' ORDER BY _score DESC LIMIT 20`。换后端 = 注册 `LanceSearch()` 还是 `VssSearch()`,管道一个字不改。

**这一节的重点是对照**:

| | `Grep`(无状态) | `Search`(服务型) |
|---|---|---|
| 常驻资源 | 无 | 引擎连接 + RAM 里的 HNSW + embedder 池 |
| 覆写的钩子 | `out_schema` + 两 `optimize_*` | 再加 `start` / `stop` / `run_duck` |
| 每次调用成本 | 就地算,自给自足 | 复用 `self.engine`;**重活在 `start`,不在 `run_duck`** |
| 关库 | 无事可做 | 必须 `stop` 释放索引内存 / 关连接 |
| 多实现 | 一个类够了 | 一父两子:公共逻辑在父类,降级各写各的 |

> 关键不变量:**`run_duck` 里绝不 `open_engine`**——那是 `start` 的活。把「开一次的贵资源」错放进 `run_duck`,就是每次查询都重载一遍索引,服务型算子的意义全丢。
>
> 这个一父两子的形状,就是**为什么 plugin 必须是类**:公共的 `out_schema` / `_query_vec` 写一遍,两个后端只写各自不同的降级。记录式在这里只能复制两份,然后它们慢慢长歪。

### 10.3 `Jq`(包一个 CLI:继承 `ExternalCommand`,几乎不用写代码)

```python
class Jq(ExternalCommand):
    name    = "jq"
    argv    = ["jq", "-c", "{script}"]                   # 基类据此替你实现 optimize_bash

    class Args(ArgSpec):
        script: str

    def out_schema(self, in_schema, args):
        return Schema.LATE                               # jq 的输出结构运行时才知道(§7)
```

用:`search cards '事故' | jq 'select(.severity>=3)' | SELECT kind, count(*) FROM _in GROUP BY kind`。默认 `read-only` 策略下 **`jq` 因 `EXEC` 被拒**(`ExternalCommand` 预设了 `caps={Cap.EXEC}`),要显式升级到 `sandboxed`/`trusted` 才能跑(operator-registry §6)。**只有 `optimize_bash`** ⇒ 它在 bash runtime 里原生,进 duck 得靠桥(vtab 包那条命令)。jq 读的 JSONL 是 `optimize_bash` 那条命令内部的事,**不用声明格式**(§4)。

### 10.4 `Rerank`(只 `run_bash`:自己 `ctx.spawn` 起子进程)—— 物化路的样子

当 bash 侧的活**不是一条干净命令**、要 Python 编排(起进程、读它的 stdout、再处理),就写 `run_bash`——它是 shell 管道里的一个进程,体内 `ctx.spawn` 拉起子进程:

```python
class Rerank(Operator):
    name = "rerank"
    caps = {Cap.EXEC}                                    # 要起子进程 ⇒ EXEC

    class Args(ArgSpec):
        model: str

    def out_schema(self, in_schema, args):
        return in_schema                                 # 只重排,列不变

    def run_bash(self, stdin, stdout, args, ctx):        # 管道里一个进程:读 stdin 流、写 stdout 流
        proc = ctx.spawn(["rerank-cli", "--model", args.model])   # ★ 经 ctx(吃 EXEC、进沙箱)
        for batch in read_jsonl(stdin):                  #   流式读上游、喂子进程、收结果
            proc.stdin.write(encode(batch))
            stdout.write(proc.stdout.read_available())
```

用:`search cards '事故' | rerank --model bge | SELECT * FROM _in LIMIT 20`。它**只有 `run_bash`**:落在 bash runtime 里是物化的一段进程;要进 duck(下游那条 SQL)由框架桥(§4 的字节↔关系 coercion)。**没有 `optimize_*` ⇒ 永远物化**——因为重排这活压根没法塞进一条 SQL / 一条命令。**这正是 `run_*` 存在的场景:表达不了原生的,就老实物化。**

## 11. 生命周期:register → start → parse → authorize → compile → run → stop

```
open(operators=[…])          实例化 + 注册(name 不撞、caps 记下);await start(ctx) 拉起常驻服务
   │
parse 管道               每段首 token 命中 registry?→ 是=算子 / 否=SQL 缺省;parse_args 校验参数
   │
authorize                算子 caps vs 策略 → allow / ask / deny(deny→编译期拒,管道不启动)
   │
compile                  runtime 指派 → 有 optimize_R 就用它产的代码;没有就用 run_R 物化(vtab/进程),
                            连 run_R 都没有就桥另一 runtime 的实现 + coercion
   │
execute                  runtime 跑:一条 DuckDB SQL 和/或一条 bash pipeline —— 不是我们跑
   │
close                    await stop():关连接、释放索引内存;无状态算子无事可做
```

作者只管覆写点;实例化 / 授权 / runtime 指派 / 沙箱 / 生命周期编排都是框架的活——它在 `open` 时替你 `start`、`close` 时 `stop`。

## 12. 测试一个算子

- **单测 `run_duck` / `run_bash`**:喂一张构造的 Arrow 表(duck)或一段 stdin 字节流(bash)+ 一个 fake `ctx`(按 caps 只放对应 helper),断言输出 schema + 行。
- **多格 differential test(准入条件,非可选)**:同一份输入,分别**强制**走该算子覆写的每一格(`optimize_duck` / `optimize_bash` / `run_duck` / `run_bash`),断言逐行一致。哪两格结果不同 = 同一条 query 换 runtime / 换格就换答案,这是本设计最危险的 bug(§3.2)。**父类可以直接提供这个测试基类**,子类只填输入——这是继承的又一处兑现。
- **服务型测生命周期**:`start` 后断言 handle 建好、多次调用**复用**同一 handle(mock 引擎断言 `open` 只调一次)、`stop` 后释放。要有实例才测得了。
- **一父多子测公共契约**:同一套断言跑在 `LanceSearch` 和 `VssSearch` 上,保证两个后端行为一致(§10.2)。
- **无界测编译期**:bash runtime 的无界 source 接一条 SQL 段,断言**编译期**报错(而不是跑起来挂住,§3.3;流式细节见 [pipeline-streaming.md](pipeline-streaming.md))。
- **降级测 `EXPLAIN`**:断言 runtime 指派符合预期(比如「加了 `optimize_duck` 之后切换点从 2 变 0」),否则性能回退无人察觉。
- **caps 负测**:给 PURE 算子的 fake `ctx` 不放 `open_read`,断言它没偷偷读盘(调了就 `CapabilityViolation`)。
- **策略测**:`read-only` 下 `EXEC`/`NET` 算子被 deny;升级后放行且仍在沙箱内。

## 13. 诚实的代价 / 边界

- **继承的老账**:基类一改,所有子类跟着动;`Operator` 的方法签名等于对外 API,轻易改不得。换来的是 §2 那五件记录式做不到的事——**只有一层继承**是控制这笔账的方式(§2)。
- **多格实现 = 多份维护 + 等价性风险**(§3.2)。只覆写少数格是完全正当的选择,它只意味着这个算子在别的 runtime 会成为切换点 / 要物化——请在文档里写明,别让用户猜为什么某条管道突然变慢。
- **只有 `run_*` 不是错,是贵**:物化一定跑得通,但它是**优化屏障**(DuckDB 看不穿 vtab、谓词推不进去、基数估计失真)+ 每批 marshal。热路径上、能表达成一条 SQL/命令的算子,值得补一个 `optimize_*`。
- **服务型 = 常驻内存 + 生命周期负担**:`search` 的引擎连接 + HNSW 索引常驻 RAM(天花板见 [search.md §5](search.md)),绑在 `open`/`close` 上——起得慢、占内存、忘了 `stop` 会漏。**够用就别做成服务型**。
- **输出 schema 是契约**:一旦有下游 SQL 依赖你的列,改列名/类型 = 破坏性变更。late-bound 更脆——所以尽量静态。
- **`run_*` 阻塞事件循环**:它是同步的,重活要走 bridge/线程(和 DuckDB 一样,见 [concurrency.md](concurrency.md))。`start`/`stop` 是 async(开/关引擎是 io)。
- **无界 source 是可选能力**:不用流就一个都没有,§3.3 那条检查是给用流的人兜底——`sh 'tail -f'` 从「挂死」变成编译期报错。真要做流式摄取见 [pipeline-streaming.md](pipeline-streaming.md)。
- **可解释性变差**:用户写 5 段管道,跑的是 1 条 SQL + 1 个子进程。报错行号、性能归因都要能映射回用户写的那一段——所以 `EXPLAIN` 是必需品,不是奢侈品。
- **不做 exactly-once**:管道中途失败 = 整条重跑。读路径无副作用所以安全;但 `FS_WRITE`/`NET` 算子**重跑就是重复副作用**——幂等性归作者,框架不担保。
- **caps 可信度**:整套授权建立在「作者诚实声明 caps」上;沙箱兜 `EXEC`/`FS_WRITE`,但弱平台上沙箱退化,此时靠策略 `deny` 名单硬关(operator-registry §8)。
- **不管数据权限**:算子契约管「算子能碰哪些资源」,**不**管「这次调用能看表 T 的哪些行」(行级授权是另一层)。

## 14. 与其他文档

- [operator-registry.md](operator-registry.md):系统视角——registry、caps 分级、能力×策略权限、沙箱(本文是它的作者侧)。
- [pipeline-as-anything.md](pipeline-as-anything.md):`_in` 表 ABI、SQL 缺省、§2.1「接缝才切」。
- [pipeline-runtime-optimize.md](pipeline-runtime-optimize.md):**本文 `optimize_*` 的去处**——宿主指派、融合切段、内联桥、代价阶梯。作者视角看「覆写几个 optimize_*」,系统视角看「编译器怎么用它们省钱」。
- [search.md](search.md):`Search` 一父两子就是那篇的**可插拔引擎**(LanceDB / DuckDB-vss)在算子层的落法(§10.2)。
- [time_machine.md](time_machine.md):`ctx.asof` 语义,source 如何下推可见性;**`ds` 不是 watermark**(§3.3 澄清)。
- [concurrency.md](concurrency.md):`run_*` 为什么重活要下沉到 bridge/线程(vtab 批回调同理)。
- [pipeline-streaming.md](pipeline-streaming.md):`bounded=False` 的无界 source + 常驻 `run_bash` sink 怎么把 bash 管道变成简易流框架(§3.3 那条延后指针的落地)。
