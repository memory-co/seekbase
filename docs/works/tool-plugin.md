# tool-plugin — 写一个工具:plugin 契约与实现

> 状态:**设计稿(pipeline 方向,未落)**。[tool-registry.md](tool-registry.md) 讲**系统视角**(registry 怎么存、权限怎么判);本文讲**作者视角**:你要给 seekbase 加一个管道工具(`search` 是内建的一个、`grep`/`find`/`sh` 是另几个),得实现一个什么样的 **plugin**?一句话:**一条注册记录 + 一个 `run(in_table, args, ctx) → out_table` 处理器**,加上诚实声明的**能力(caps)**和**输出 schema**。
>
> 依赖:[tool-registry.md](tool-registry.md)(Tool 记录、caps 分级、能力×策略权限)、[pipeline-as-anything.md](pipeline-as-anything.md)(`_in` 表 ABI、source/transform/tool/sink 类别、SQL 是缺省)。

## 1. 定位:一个 tool = 一个 plugin

管道里每段非-SQL 的 verb 都由一个 plugin 支撑。一个 plugin 就是一条**注册记录**:

```python
Tool(
    name    = "grep",
    kind    = "tool",                       # source | tool | sink(见 §8;transform=SQL 不是 plugin)
    args    = "<pattern> [--field <col>]",  # 供解析 + --help 的签名
    caps    = {Cap.PURE},                   # 诚实声明碰什么外界资源(§6)
    out     = grep_out_schema,              # 输出列 schema(静态声明或 late-bound,§7)
    run     = grep_run,                     # ★ (in_table, args, ctx) -> out_table
)
```

作者要填的就这几格,重点是 **`run`**(§3)、**`caps`**(§6)、**`out`**(§7);**服务型工具**(背后有常驻进程,如 `search`)还要加 **`start`/`stop`** 生命周期(§3.1)——`grep` 这类无状态工具不用。下面逐个拆。

## 2. 三种作者形态(按复杂度挑)

| 形态 | 怎么写 | 适合 | 序列化 |
|---|---|---|---|
| **函数式(最小)** | 一个 `Tool(..., run=fn)`,`fn` 收/返 Arrow 表 | PURE / 轻量、进程内(grep、sed-over-table) | 无(进程内零拷) |
| **类式(Protocol)** | 实现 `ToolPlugin` 协议(`name/kind/caps/out_schema/run` + 可选 `start/stop`) | 要参数校验、动态 schema、或**带常驻服务/进程的工具**(如 `search`,§3.1) | 无 |
| **外部命令式** | `ExternalTool(name, argv, caps, encode='arrow'\|'jsonl')` | 包一个现成 CLI(`jq`、任意脚本) | 框架管(表 ↔ stdin/stdout) |

- **外部命令式作者不碰序列化**:你只给 `argv` 模板 + 编码格式,框架把 `_in` 序列化进 stdin、把 stdout 解析回表(见 [pipeline-as-anything.md §4](pipeline-as-anything.md))。这是把任意 CLI 变工具的最省路径,代价是它天然带 `EXEC` 能力、默认受最严策略约束(§6)。
- **函数式/类式是进程内的**:收/返的是 Arrow-backed 关系,和下游 DuckSQL 段零拷交换。

## 3. 核心契约:`run(in_table, args, ctx) → out_table`

所有形态最终归到这一个签名:

```python
def run(in_table: Table | None, args: Args, ctx: ToolCtx) -> Table:
    ...
```

- **`in_table`**:上一段的产物 `_in`(pipeline §2 的表 ABI)。**`kind="source"` 时为 `None`**(source 无输入、自己产表);`tool`/`sink` 时是一张 Arrow-backed 关系。**只读**——不要原地改它,产一张新表返回。
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
  | `run(in_table, args, ctx)` | 每次调用 | **复用** handle(不重开),做一次检索/变换 |
  | `stop(handle)` | `close` 时一次 | 拆常驻服务:关连接、释放索引内存 |

  用**类式** plugin 承载最自然:实例在 `start` 里把 handle 存字段、`run` 里复用、`stop` 里拆。无状态工具则这两个钩子都不实现——框架看到没有 `start` 就当它零常驻。

> 分界线:**「每次调用要不要复用一份贵的、开一次的资源」**。要 → 服务型(start/run/stop);不要 → 无状态(只 run)。`search` 的引擎 + RAM 常驻索引就是那份贵资源,`grep` 什么都不用留。

## 4. 表 ABI:你收到什么、要返回什么

- **进程内(函数式/类式)**:`in_table` / 返回值都是 **Arrow 表**(或等价的 DuckDB 关系句柄)。列名 = `_in` 的列;`source` 的输出列由你定(如 `search` 产 `(pk, _score, …)`)。和下游 SQL 段**零拷**(同一 DuckDB 连接里挂成临时视图)。
- **外部命令式**:你**看不到** `Table` 对象——框架把 `in_table` 编码成 **Arrow IPC**(优先,带类型)或 **JSONL**(退化,人可读)喂 stdin,把你 stdout 的字节解析回表。你只写「读 stdin 的行、吐 stdout 的行」的普通 CLI。
- **恒名 `_in`**:你不需要知道上一段是谁——只认 `_in` 这张表。这让工具**可组合、可换位**(`grep` 既能跟在 `search` 后、也能跟在 `read` 后)。

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

## 8. kind 差异:source / tool / sink(transform 不是 plugin)

| kind | `in_table` | 返回 | 位置 | 例 |
|---|---|---|---|---|
| `source` | `None` | 一张新表 | 只能打头 | `search`(服务型) `scan` `read` |
| `tool` | `_in` | 变换后的表 | 中间 | `grep` `sed` `http` `embed` `sh` |
| `sink` | `_in` | 回给调用方的 rows(可无表返回) | 只能收尾 | `emit`(默认末端) |

- **`source` 可读 `ctx.asof`**:把时光机 as-of 下推进自己的候选(`search` 见 [search.md §6](search.md),`scan` 见 [time_machine.md](time_machine.md))。
- **transform ≠ plugin**:一整条 DuckDB SQL(over `_in`)不是工具,是管道缺省(pipeline §2.1)——首 token 不命中 registry 的段就是 SQL,不用也不能注册成 plugin。

## 9. 注册:挂进 registry

```python
db = await Seekbase.open("./data", schema=SCHEMA, tools=[
    Tool(name="grep", kind="tool", args="<pattern> [--field <col>]",
         caps={Cap.PURE}, out=grep_out_schema, run=grep_run),
    SearchSource(),          # 类式 plugin 实例(服务型:带 start/run/stop,§10.2)
    ExternalTool("jq", argv=["jq", "-c", "{arg0}"], caps={Cap.EXEC}, encode="jsonl"),
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

Tool(name="grep", kind="tool", args="<pattern> [--field <col>]",
     caps={Cap.PURE}, out=grep_out_schema, run=grep_run)      # 只注册一个 run
```

用:`search cards '事故' | grep 'ERROR' --field issue | SELECT card_id FROM _in LIMIT 20`。**没有任何常驻状态**:每次调用自给自足,关库时也无从拆起。

### 10.2 `search`(最复杂:服务型、有常驻进程)—— start / run / stop

`search` 复杂**不在算法**(RRF 就那样),而在**它是个服务**:背后一个常驻的向量引擎 + 载进 RAM 的 HNSW 索引 + embedder 连接池,**开一次、每次 `run` 复用**。所以它实现完整的 `start`/`run`/`stop`:

```python
class SearchSource:                                    # 类式:实例持有常驻 handle
    name, kind, caps = "search", "source", {Cap.PURE}  # 引擎在库内、检索不碰外界 = PURE
                                                       # (embedder 是注入的常驻服务,自身的 NET 由 ctx 中介)
    def out_schema(self, _in, args):                   # source:in_schema 为 None
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
ExternalTool("jq", argv=["jq", "-c", "{arg0}"],        # {arg0} = 用户传的 jq 脚本
             caps={Cap.EXEC}, encode="jsonl")          # 框架:_in→JSONL→stdin,stdout→表
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
- **caps 负测**:给 PURE 工具的 fake `ctx` 不放 `open_read`,断言它没偷偷读盘(调了就 `CapabilityViolation`)。
- **管道集成**:`db.query("<tool> … | SELECT …")` 跑通,验证下游 SQL 能引用你声明的 `out` 列;late-bound 工具额外验证运行期 schema。
- **策略测**:`read-only` 下 `EXEC`/`NET` 工具被 deny;升级后放行且仍在沙箱内。

## 13. 诚实的代价 / 边界

- **输出 schema 是契约**:一旦有下游 SQL 依赖你的 `out` 列,改列名/类型 = 破坏性变更。late-bound 工具把这份契约推到运行时,更脆——所以尽量静态。
- **服务型工具 = 常驻内存 + 生命周期负担**:`search` 的引擎连接 + HNSW 索引常驻 RAM(天花板见 [search.md §5](search.md)),绑在 `open`/`close` 上——起得慢(`start` 载索引)、占内存、忘了 `stop` 会漏。无状态工具(`grep`)零常驻,没这些账。**够用就别做成服务型**:只有「每次调用要复用一份开一次的贵资源」才值得背 start/stop。
- **进程内工具阻塞事件循环**:函数式 `run` 是同步的,重活要走 bridge/线程(和 DuckDB 一样,见 [concurrency.md](concurrency.md)),否则卡住 async 门面。服务型的 `start`/`stop` 是 async(开/关引擎是 io)。
- **外部命令式的 marshalling 成本**:每进出一次子进程,表就序列化一次(pipeline §9);热路径别滥用 `sh`,能用进程内 PURE 工具就用。
- **caps 可信度**:整套授权建立在「作者诚实声明 caps」上;沙箱兜 `EXEC`/`FS_WRITE`,但弱平台上沙箱退化,此时靠策略 `deny` 名单硬关(tool-registry §8)。
- **不管数据权限**:plugin 契约管「工具能碰哪些资源」,**不**管「这次调用能看表 T 的哪些行」(行级授权是另一层)。

## 14. 与其他文档

- [tool-registry.md](tool-registry.md):系统视角——registry、caps 分级、能力×策略权限、沙箱(本文是它的作者侧)。
- [pipeline-as-anything.md](pipeline-as-anything.md):`_in` 表 ABI、source/transform/tool/sink、SQL 缺省、段间序列化。
- [search.md](search.md):内建 `search` source 就是一个 plugin(可插拔引擎),可当范例。
- [time_machine.md](time_machine.md):`ctx.asof` 语义,source 工具如何下推可见性。
- [concurrency.md](concurrency.md):进程内 `run` 为什么重活要下沉到 bridge/线程。
