# pipeline-as-anything — 用管道串联一切(SPL 式 query)

> 状态:**设计稿(未落)**。一个和现状不同方向的推演:不再把 `search()` 做成内嵌 SQL 的 UDF,而是把 **query 本身变成一串管道**——`stage | stage | stage`,每个 stage 都是「吃一张表、吐一张表」的算子。搜索只是其中一个 **source 算子**(用 LanceDB 搜出来 → 物化成结果表 → 交给 DuckDB 的 `SELECT` 去查);同一根管道里还能串 `bash`、HTTP、embed 等任意 tool。于是 seekbase 的 query 就成了一种 **SPL(Splunk 式 search processing language)**:检索是管道的一段,不是 SQL 里的一个函数。
>
> 本文只推演设计,不描述已落代码。对照:现状把 `search()` 做成 SQL 一等算子、单引擎 DuckDB,见 [search.md](search.md);那条路的重写/缝合之痛(§3)正是本稿要绕开的东西。

## 1. 一句话:query 从「一条 SQL」变成「一串管道」

现状(search.md):一条 DuckDB SQL,`search(列,'文本')` 是嵌在 `WHERE` 里的语法糖,查询前要**重写 + 缝合**(抽占位 → 算候选 → `LEFT JOIN` 缝回)。

本稿:query 是一根**管道**,`|` 分段,数据在段与段之间以**表(关系)**流动:

```
search cards "pty 终端"                             │ source:LanceDB hybrid 搜 → 结果表 _in(pk,_score,…)
  | SELECT * FROM _in WHERE kind='issue'            │ transform:一整条 DuckDB SQL,吃上一段的表 _in
        ORDER BY _score DESC LIMIT 20               │   (WHERE/ORDER BY/LIMIT 是 SQL 自己的活,不拆成段)
```

搜索退回成管道的**第一段**(一个 source),产物是一张普通表 `_in`;**后面就是一整条 DuckDB SQL**,在 `_in` 上跑。注意 `WHERE`/`ORDER BY`/`LIMIT` **没有**被切成 `where | sort | limit` 三段——那是一条 DuckDB SQL 自己的活,切开只是把 SQL 重写成更弱的管道 DSL。这里只有一个 `|`,因为只有一道接缝:**搜索(lance)和 SQL(duck)之间**这道 DuckDB 自己跨不过去的界。**没有 `search()` 这个函数了**——搜索不再藏进 SQL,它就是接缝前的那一站。

## 2. 核心不变量:一切皆表(表就是 stage 之间的 ABI)

管道能成立,靠一条铁律:**每个 stage 的输入和输出都是一张关系(表)**。这就是 stage 之间唯一的接口契约(ABI)。

```
        ┌────────┐   表    ┌────────┐   表    ┌────────┐   表    ┌────────┐
 (无) → │ source │ ──────→ │transform│ ──────→ │  tool  │ ──────→ │  sink  │ → 结果
        └────────┘         └────────┘         └────────┘         └────────┘
         search             where/sort          sh/http           select/收集
```

- **source**:无输入、产一张表——`search`(LanceDB)、`scan <表>`(直接读业务表)、`read <文件>`。
- **transform**:表进表出、**一整条**纯 DuckDB SQL over `_in`——`SELECT … FROM _in WHERE … ORDER BY … LIMIT …`,过滤/排序/聚合/join/窗口一段做完(**不**拆成 verb 段,见 §2.1)。
- **tool**:表进表出、但**跳出 DuckDB**——`sh <命令>`(shell)、`http <url>`、`embed <列>`…… 吃上一段的表(序列化进 stdin)、把产物解析回表。
- **sink**:管道末端,把最终表交回调用方(`rows`)。

只要一个东西**能吃一张表、能吐一张表**,它就能当 stage 挂进管道。搜索、SQL、shell、HTTP —— 在这条铁律下是**同一种公民**。这就是 "pipeline as anything"。

### 2.1 「一切皆表」不等于「一切都切成段」

这是这套设计最容易被滥用的地方,先钉死:**`|` 标的是 DuckDB 自己跨不过去的接缝**(lance→duck、duck→bash、duck→http),**不是**语法便利。一条 SQL 能干的事(`WHERE`/`ORDER BY`/`LIMIT`/`JOIN`/聚合/窗口),就**在一段 SQL 里干完**,绝不拆成 `where | sort | limit`——那只是把 SQL 重写成一个更弱的管道 DSL,纯亏。推论:

- **一段 DuckDB stage = 一整条 DuckDB SQL**(over `_in`),不是 verb 链。DuckDB 认得的,原样交给它。
- **纯 SQL query 有零个 `|`**:没有 search source、没有 tool,它就**根本不是管道**——直接 `db.query(sql)`,该走 search.md 那条路就走那条。
- 管道是 **opt-in**:**只有**当你要跨 lance / bash / http 这类 DuckDB 进不去的地界,才付出一个 `|`。切段的**唯一理由是跨引擎/跨进程**;为「看起来像 SPL」而切,是自找复杂度。
- **SQL 是缺省**:一段的首 token 不命中任何注册工具 → 它**就是** SQL(§6),不是错误。工具是例外、SQL 是常态——这也是「SQL 一等公民」的落地。

> 换句话说:**接缝才切,SQL 之内不切**。`SELECT * FROM cards WHERE kind='issue' LIMIT 20` 这种 DuckDB 天生能查的,永远是一条 SQL、零管道;管道只在它跨不过去的那一刻才出现。

## 3. 动因:为什么废掉 `search()` UDF

现状把 `search()` 内嵌进 SQL,代价集中在 search.md §3 那套**重写 + 缝合**:

| 现状 `search()` UDF 的痛(search.md §3) | 管道模型怎么消掉它 |
|---|---|
| `search(列,'文本')` 不是 DuckDB 函数,query 前要**正则抽取**占位、替换成 `(_score_<列> IS NOT NULL)` | 搜索是**独立 source**,产物是真表;不用把它塞进 SQL,也就不用把它从 SQL 里抠出来 |
| 抽完要**定表**(`search_target`)、算候选、灌**临时表**、再 `LEFT JOIN` 可见性视图缝回 | source 直接**物化成一张命名表**,下一段 `FROM` 它即可,零缝合 |
| 正则有边界情况:SQL 注释里的 `search(...)` 误判、`search(列, ?)` 参数绑定不支持(search.md §3.3 实现说明) | 搜索参数是 stage 的**普通参数**(`search cards ?`),天然可绑定;不再从 SQL 文本里抠字面量 |
| 多个 `search()` 要各自 `_score_<列>`、同名加后缀去重 | 多次搜索 = 管道里多段 source(或分支),各自成表,不抢命名空间 |
| 引擎被焊死在 SQL 重写链里,换引擎要动重写层 | 引擎藏在 source stage 背后,可插拔(§8) |

一句话:`search()` UDF 的复杂度全在**「把非-SQL 的检索硬缝进 SQL」**。管道模型把这道缝**从 SQL 内部挪到 stage 边界**——搜索产表、SQL 读表,两边都干净。

## 4. 算子怎么交换表(进程内零拷,跨进程序列化)

stage 之间流动的是**物化关系**,按下一段是不是还在 DuckDB 里,分两条通道:

- **DuckDB → DuckDB**:产物是一张 **DuckDB 关系**(temp view / Arrow),下一段 SQL 直接 `FROM _in`,同连接内零拷、零序列化。约定**上一段的表恒名 `_in`**。注意:相邻两段若都在 duck 里,本就**该合成一条 SQL**(§2.1)——之所以还会出现相邻 duck 段,只因为中间夹了个 tool,tool 前后各一条 SQL。
- **DuckDB ↔ tool(sh/http)**:跨出进程,必须**序列化**。表以 **Arrow IPC**(优先,带类型)或 **JSONL**(退化,人可读)写进 tool 的 stdin;tool 的 stdout 再解析回 DuckDB 表(`read_json_auto` / Arrow 读)。这是管道里唯一有 marshal 成本的接缝。

```
search ─(DuckDB 表 _in)→ [ 一条 SQL over _in ]                              ← 段内零拷,全在一个 duck 连接里
   └───(Arrow/JSONL over stdin)→ sh 'jq …' ─(stdout→表 _in)→ [ 一条 SQL ]   ← 只有跨进程这一步才序列化
```

- **source 的产物也物化成 `_in`**:`search` 把 LanceDB 返回的 `[(pk, score, …)]` 灌进一张 temp 表(就叫 `_in`),下一段照常 `FROM _in`——和 search.md §3.3 的临时表是同一手法,区别是**它现在是管道的正式产物,不是缝进外层 SQL 的旁路**。
- **表就是 stage 的返回值**:每段执行完把 `_in` 重绑到自己的产物,管道就是「不断重绑 `_in`」的折叠。

## 5. 走一遍:两条管道逐段看表的形状

**(a) 检索 + 结构化**——一个 search source + 一条 SQL,共 **2 段、1 个 `|`**:

```
search cards "pty 终端"
  | SELECT * FROM _in WHERE kind='issue' ORDER BY _score DESC LIMIT 20
```

| 段 | 干了什么 | `_in` 变成 |
|---|---|---|
| `search cards "pty 终端"` | embed + jieba 分词 → LanceDB(或 duck-vss)hybrid RRF,物化成表 | `_in(card_id, issue, kind, _score, …)` — 命中集 |
| `SELECT … FROM _in WHERE … ORDER BY … LIMIT` | **一整条** DuckDB SQL 吃 `_in`,过滤 + 排序 + 截断一次做完 | 最终 20 行 |

> 只有一个 `|`,因为只有一道接缝:lance 的命中集要交给 duck。**若这条 query 不需要 search**(纯结构化查询),它就**不是管道**——`SELECT * FROM cards WHERE kind='issue' ORDER BY created_at DESC LIMIT 20` 一条 SQL 直接 `db.query()`,零 `|`。别为了像 SPL 把它切开(§2.1)。

**(b) 串 tool 的管道**——检索完丢给 shell 处理,再回 DuckDB 聚合:

```
search cards "线上事故"
  | sh 'jq -c "select(.severity>=3)"'      ← shell 过滤,表以 JSONL 过 stdin/stdout
  | select kind, count(*) group by kind    ← 回到 DuckDB 聚合
```

`sh` 段把 `_in` 序列化成 JSONL 喂给 `jq`,`jq` 的 stdout 解析回表,再交给下一段的 DuckDB SQL。**shell 和 SQL 在同一根管道里无缝接力**——这是 `search()` UDF 永远做不到的:UDF 活在一条 SQL 里,出不去。

## 6. 语法与编译:SPL 在前,DuckDB 在后

管道文法极简:`pipeline := stage ('|' stage)*`。**SQL 是一等公民、也是缺省**:解析一段时只看它的**首 token**——命中 tool registry(见 [tool-registry.md](tool-registry.md))里的工具名(`search`/`scan`/`sh`/`grep`…)→ 走那个工具;**否则整段当一条 DuckDB SQL 执行**。所以工具是「首 token 匹配才走」的特例,SQL 是不匹配时的默认——不存在「未知工具」,不匹配即 SQL。SQL 的引导关键字(`SELECT`/`WITH`/`FROM`)不会和工具名相撞,天然无歧义。编译分工:

- **transform 段就是一条 DuckDB SQL**(over `_in`):原样交给 DuckDB,seekbase **不重写、不拆 verb**。`WHERE`/`ORDER BY`/`LIMIT`/`JOIN`/CTE/窗口全是 SQL 自己的事——管道**不发明** `where|sort|limit` 这种更弱的语法糖去替 SQL(§2.1 禁的就是这个)。管道对这一段的全部职责,就是把 `_in` 递进去。
- **source 段调引擎**:`search` → `StoreService.hybrid`(或 LanceDB 客户端)→ 物化 `_in`;`scan <表>` → 就是 `_in := <可见性视图>`。
- **tool 段起子进程**:`sh` → 序列化 `_in` → `subprocess` → 解析 stdout → 重绑 `_in`。

整条管道编译成一串「对 `_in` 的操作」,**逐段折叠**执行——没有跨全句的 SQL 重写,每段是自洽的小步。对比 search.md §3 的「一条外层 SQL 里塞占位再缝」,这里是「N 段各自成表、顺次接力」。

## 7. 时光机怎么进管道

现状(time_machine.md):`ds_start`/`ds_end` 作用于外层可见性视图,search 候选和外层**共用同一 as-of 谓词**。管道模型里它落成 **source 段的参数**:

```
scan cards @asof=20260601        ← _in := cards 在 20260601 的存活行(as-of 可见性视图)
  | search-within "pty 终端"      ← 只在这张历史快照上检索
```

- `@asof` 挂在 source 上,决定 `_in` 的初始可见性;后续 transform 段在这张历史表上跑,时光机语义天然继承(它们只看得见 `_in`)。
- search 作为 source 时,as-of 谓词下推进 LanceDB/vss 候选(和 search.md §4 的 `<可见性谓词>`、over-fetch ×2 同一套逻辑)——**回溯到某天,搜的也是那天的存活集**。语义不变,只是接线从「缝进外层」变成「source 的入参」。

## 8. 搜索引擎可插拔:LanceDB 的回归(诚实讲代价)

stage 边界把**引擎**关进了 source 段背后:`search` 的产物只承诺是一张 `(pk, score, …)` 表,**背后是 LanceDB 还是 DuckDB-vss,管道不关心**。这正是本稿敢重新用 LanceDB 的原因——它被隔离在一个 stage 里,不再和结构化 SQL 焊在同一条链上。

但要**诚实**:search.md §6 收掉 LanceDB 是有原因的——它**版本化、每写生成碎片、每操作开句柄**,在 memory.talk 里反复撞 `Too many open files (EMFILE)`,背了一整套 compaction + 重连的恢复机械。把它作为管道 source 请回来,**那套 fd 代价也一起回来**。取舍要摆明:

| | LanceDB 当 search source | DuckDB-vss 当 search source(现状引擎) |
|---|---|---|
| fd | 碎片文件 + 句柄,EMFILE 风险回归,需 compaction/重连机械 | 单文件、fd 恒定(search.md §6) |
| 隔离 | 关在 stage 背后,不污染结构化侧 | 本来就单引擎 |
| 段间交换 | 跨引擎,产物要物化成 DuckDB 表(一次拷贝) | 同引擎,temp view 零拷 |
| 何时值得 | 需要 Lance 的版本化/列存/独立扩缩时 | 写少读多、内存可控的 memory 场景 |

**本稿的立场**:管道**不绑定**任何一个搜索引擎——`search` 是接口,LanceDB 和 DuckDB-vss 都是它的实现。示例用 LanceDB 只为说明「跨引擎 source 也能无缝物化成表」;真要用,先认领 §6 那张表里的 fd 账。

## 9. 代价 / 边界 / 没选的

诚实讲管道模型不是免费的:

- **失去全局优化**:一条 SQL 里,DuckDB 优化器能跨 `search` 缝合点做谓词下推、join 重排;拆成 N 段后,**段与段之间是优化墙**——`where` 在 `search` 之后跑,搜出 200 条再过滤,而不是把过滤下推进检索。缓解:让 source 段吃常见谓词(`search cards "…" where kind='issue'`),把能下推的下推;剩下的接受墙。
- **tool 段是安全洞**:`sh <任意命令>` = 在 query 里执行 shell。**必须**沙箱 / 命令白名单 / 显式开关,默认关。否则 query 接口等于 RCE。这是 `search()` UDF 从不会有的风险面,管道换来的表达力得用围栏还回去——这道围栏正是 [tool-registry.md](tool-registry.md) 的**能力(capability)× 策略(policy)**机制:默认 `read-only`、`EXEC` 类工具默认关、放行也只在沙箱里跑。
- **序列化成本**:每进出 tool 一次,表就 marshal 一次(§4)。纯 DuckDB 段之间零拷,但 shell 密集的管道要认这笔账;Arrow IPC 比 JSONL 省,优先它。
- **没选「管道取代 SQL」**:transform 段**就是** DuckDB SQL,不套 verb 外壳、不拆段(§2.1)。管道只在 **source/tool 的接缝**上加一根组合轴,不另造查询语言。能一条 SQL 查完的,就一条 SQL——管道对它**不插手、也不该出现**;它只在 DuckDB 跨不过去的那一刻登场。

**为什么仍值得**:它把 search.md §3 那套「非-SQL 检索硬缝进 SQL」的复杂度**从 SQL 内部搬到 stage 边界**,顺带让 shell / HTTP / embed 变成一等公民——query 从「带一个魔法函数的 SQL」升级成「能串任意 tool 的 SPL」。检索只是第一个 source;真正的收益是**这根管道后面能挂什么**。

## 10. 与现有架构的接缝(若要落地)

- `ReadService.query`(architecture.md §2)从「rewrite + 单条 SQL 执行」改成「**管道编译器 + 逐段执行器**」:解析 `|` → 编译每段 → 折叠重绑 `_in`。
- rewrite 层(`extract_searches` / `search_target` / 缝合,search.md §3)**整体退休**——不再有 `search()` 要抽。
- `StoreService.hybrid`(search.md §4)从「被外层 SQL 缝合的旁路」变成「`search` source stage 的引擎后端」,接口不变(吃向量/token,吐 `(pk,score)`),换的是调用位置。
- 新增 **tool registry + 执行器**(subprocess + Arrow/JSONL 编解码)和它的**能力/策略/沙箱围栏**(见 [tool-registry.md](tool-registry.md))。`search` 也注册进这张表——它只是一个 source 工具,不特殊。
- 两形态(嵌入 / HTTP)接缝不动:管道字符串照样过 `Request`,`LocalExecutor` / `HttpExecutor` 走同一个管道执行器。

## 11. 与其他文档

- [search.md](search.md):`search` 作为一个 source 段 + **可插拔引擎**(lance / duck-vss),复用的 RRF/jieba(§3/§4)、要认领的 LanceDB fd 账(§5)。
- [tool-registry.md](tool-registry.md):`search` 只是众多**注册工具**之一(`grep`/`find`/`sed`/`sh` 平级);tool 段的注册契约 + 能力/策略/沙箱围栏(§9 安全担忧的答案)。
- [architecture.md](architecture.md):读链 = `PipelineService` 管道编译器(rewrite 层退休)。
- [time_machine.md](time_machine.md):as-of 可见性谓词,变成 source 段的 `@asof` 入参(§7)。
- [store.md](store.md):派生层 = 结构化 DuckDB + 可插拔检索后端;可见性视图 / `scan` source 直接读它。
- [../../DESIGN.md](../../DESIGN.md):整体工程设计与分期。
