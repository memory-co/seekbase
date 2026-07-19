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

作者要填的就这几格,重点是 **`run`**(§3)、**`caps`**(§6)、**`out`**(§7)。下面逐个拆。

## 2. 三种作者形态(按复杂度挑)

| 形态 | 怎么写 | 适合 | 序列化 |
|---|---|---|---|
| **函数式(最小)** | 一个 `Tool(..., run=fn)`,`fn` 收/返 Arrow 表 | PURE / 轻量、进程内(grep、sed-over-table) | 无(进程内零拷) |
| **类式(Protocol)** | 实现 `ToolPlugin` 协议(`name/kind/caps/parse_args/out_schema/run`) | 要参数校验、动态输出 schema、带状态的 source | 无 |
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
| `source` | `None` | 一张新表 | 只能打头 | `search` `scan` `read` `rss` |
| `tool` | `_in` | 变换后的表 | 中间 | `grep` `sed` `http` `embed` `sh` |
| `sink` | `_in` | 回给调用方的 rows(可无表返回) | 只能收尾 | `emit`(默认末端) |

- **`source` 可读 `ctx.asof`**:把时光机 as-of 下推进自己的候选(`search` 见 [search.md §6](search.md),`scan` 见 [time_machine.md](time_machine.md))。
- **transform ≠ plugin**:一整条 DuckDB SQL(over `_in`)不是工具,是管道缺省(pipeline §2.1)——首 token 不命中 registry 的段就是 SQL,不用也不能注册成 plugin。

## 9. 注册:挂进 registry

```python
db = await Seekbase.open("./data", schema=SCHEMA, tools=[
    Tool(name="grep", kind="tool", args="<pattern> [--field <col>]",
         caps={Cap.PURE}, out=grep_out_schema, run=grep_run),
    my_rss_source,           # 类式 plugin 实例
    ExternalTool("jq", argv=["jq", "-c", "{arg0}"], caps={Cap.EXEC}, encode="jsonl"),
])
```

- **名字规则**:不取 SQL 引导关键字(`select`/`with`/`from`…),否则会和「SQL 缺省」相撞(tool-registry §5);和内建/已注册**同名 → 显式报错**,不覆盖。
- **内建 + 用户注册同一张表**:你的工具和 `search` 平权;用户工具**必须声明 caps**,进不了「审过」名单、默认按声明 caps 受策略约束 + 沙箱。

## 10. 三个完整例子

### 10.1 `grep`(函数式,PURE)—— 表内正则过滤

```python
def grep_out_schema(in_schema, args):
    return in_schema                                   # 只过滤行,列不变

def grep_run(in_table, args, ctx):                     # PURE:ctx 只有 asof/deadline,无 io
    col = args.get("field")                            # None = 所有文本列
    pat = re.compile(args["pattern"])
    return in_table.filter(lambda row: any(
        pat.search(str(row[c])) for c in (col and [col] or in_table.text_cols)))

Tool(name="grep", kind="tool", args="<pattern> [--field <col>]",
     caps={Cap.PURE}, out=grep_out_schema, run=grep_run)
```

用:`search cards '事故' | grep 'ERROR' --field issue | SELECT card_id FROM _in LIMIT 20`。

### 10.2 `rss <url>`(类式 source,NET)—— 拉取成表

```python
class RssSource:
    name, kind, caps = "rss", "source", {Cap.NET}
    def out_schema(self, _in, args):                   # source:in_schema 为 None
        return Schema([("id","str"), ("title","str"), ("published","timestamptz")])
    def run(self, in_table, args, ctx):                # in_table 恒 None(source)
        resp = ctx.http(Req("GET", args["url"]))       # ← 过 ctx,受 NET 策略/沙箱
        return rows_to_table(parse_feed(resp.body), self.out_schema(None, args))
```

用:`rss 'https://…/feed' | SELECT title FROM _in WHERE published > now() - INTERVAL 1 DAY`。

### 10.3 `jq`(外部命令式,EXEC/沙箱)—— 包一个 CLI

```python
ExternalTool("jq", argv=["jq", "-c", "{arg0}"],        # {arg0} = 用户传的 jq 脚本
             caps={Cap.EXEC}, encode="jsonl")          # 框架:_in→JSONL→stdin,stdout→表
```

用:`search cards '事故' | jq 'select(.severity>=3)' | SELECT kind, count(*) FROM _in GROUP BY kind`。默认 `read-only` 策略下 **`jq` 因 `EXEC` 被拒**,要显式升级到 `sandboxed`/`trusted` 才能跑(tool-registry §6)——作者无需为权限操心,策略层统一管。

## 11. 生命周期:register → resolve → authorize → execute

```
open(tools=[…])          注册:进 registry,name 不撞、caps 记下
   │
parse 管道               解析:每段首 token 命中 registry?→ 是=工具 / 否=SQL 缺省
   │
compile-time authorize   授权:工具 caps vs 策略 → allow / ask / deny(deny→编译期拒,管道不启动)
   │
execute(fold _in)        执行:tool.run(_in, args, ctx),ctx 按已授 caps 装 helper + 沙箱
```

作者只管 `run`;resolve / authorize / 沙箱都是框架的活。

## 12. 测试一个 tool

- **单测 `run`**:喂一张构造的 Arrow 表 + 一个 fake `ctx`(按 caps 只放对应 helper),断言输出表的 schema + 行。
- **caps 负测**:给 PURE 工具的 fake `ctx` 不放 `open_read`,断言它没偷偷读盘(调了就 `CapabilityViolation`)。
- **管道集成**:`db.query("<tool> … | SELECT …")` 跑通,验证下游 SQL 能引用你声明的 `out` 列;late-bound 工具额外验证运行期 schema。
- **策略测**:`read-only` 下 `EXEC`/`NET` 工具被 deny;升级后放行且仍在沙箱内。

## 13. 诚实的代价 / 边界

- **输出 schema 是契约**:一旦有下游 SQL 依赖你的 `out` 列,改列名/类型 = 破坏性变更。late-bound 工具把这份契约推到运行时,更脆——所以尽量静态。
- **进程内工具阻塞事件循环**:函数式 `run` 是同步的,重活要走 bridge/线程(和 DuckDB 一样,见 [concurrency.md](concurrency.md)),否则卡住 async 门面。
- **外部命令式的 marshalling 成本**:每进出一次子进程,表就序列化一次(pipeline §9);热路径别滥用 `sh`,能用进程内 PURE 工具就用。
- **caps 可信度**:整套授权建立在「作者诚实声明 caps」上;沙箱兜 `EXEC`/`FS_WRITE`,但弱平台上沙箱退化,此时靠策略 `deny` 名单硬关(tool-registry §8)。
- **不管数据权限**:plugin 契约管「工具能碰哪些资源」,**不**管「这次调用能看表 T 的哪些行」(行级授权是另一层)。

## 14. 与其他文档

- [tool-registry.md](tool-registry.md):系统视角——registry、caps 分级、能力×策略权限、沙箱(本文是它的作者侧)。
- [pipeline-as-anything.md](pipeline-as-anything.md):`_in` 表 ABI、source/transform/tool/sink、SQL 缺省、段间序列化。
- [search.md](search.md):内建 `search` source 就是一个 plugin(可插拔引擎),可当范例。
- [time_machine.md](time_machine.md):`ctx.asof` 语义,source 工具如何下推可见性。
- [concurrency.md](concurrency.md):进程内 `run` 为什么重活要下沉到 bridge/线程。
