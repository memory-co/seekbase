# pipeline-runtime-optimize — 管道不自己跑:降级到 DuckDB `WITH` / bash pipeline,以及怎么少花钱

> 状态:**设计稿(pipeline 方向,未落)**。[pipeline-as-anything.md](pipeline-as-anything.md) 定的是**前端**(query = `stage | stage`);本文定**后端**:这根管道**不由 seekbase 自己执行**,而是被**降级(lowering)**到一个 **pipeline runtime(运行时/宿主)**。runtime 是一个**开放集**——今天有两个(**DuckDB 的 `WITH` 链**、**bash 的 pipeline**),后续可以再加(§10)。算子的物化 `run_duck`/`run_bash` **不是**第三个 runtime,而是**两个 runtime 各自的物化兜底**(duck 里当 vtab、bash 里当读 stdin 的进程,§2)。我们不造管道运行时,我们造一个**面向多 runtime 的编译器**。
>
> 于是「优化」有了精确含义:**同一条 query 有多种降级方案(落到哪个 runtime、在哪切),成本差一个数量级,编译器要挑便宜的那个。** 本文就是这套挑法。
>
> 依赖:[pipeline-as-anything.md](pipeline-as-anything.md)(前端语法、`_in` ABI、§2.1「接缝才切」)、[operator-plugin.md](operator-plugin.md)(算子契约:两轴四方法——原生 `optimize_duck`/`optimize_bash` × 物化 `run_duck`/`run_bash`)。

## 1. 前提:管道是编译前端,不是运行时

```
   前端(用户写的)              编译                     后端(真正跑的)
                              ┌──────────────────────────────────────────┐
  search … | grep … | SELECT  │  宿主指派 → 融合 → codegen               │
                              └──────────────────────────────────────────┘
                                          ↓                    ↓
                              WITH s0 AS (…),          cmd1 | cmd2 | cmd3
                                   s1 AS (…)           (内核管道:背压/流式白送)
                              SELECT … FROM s1
                              (一条 SQL:全局优化白送)
```

- **管道里的 `|` 大多数会被编译掉**。同 runtime 的相邻段之间,`|` 只是语法——落到 `WITH` 里是一个 CTE 边界(DuckDB 优化器**看穿**它),落到 bash 里是一个真管道符(内核给背压)。
- **只有跨 runtime 的 `|` 是真接缝**。这给了 pipeline §2.1「接缝才切」一个可计算的定义:**真接缝 = runtime 切换点**,数量由编译器算出来,不由用户写出来。
- **每个 runtime 各自带来白送的东西**:DuckDB `WITH` = 跨段谓词下推 / join 重排 / 基数估计;bash pipeline = 背压、增量、无界流。**这正是不自己造运行时的理由**——自己造的话这些都得重写一遍,还写不过它们。

> **术语:本文的「宿主(host)」= 「pipeline runtime」,同一个东西的两个叫法。** 下文混用,都指「承载一段(或整条)管道执行的那个引擎」。

### 1.1 两层,别混:pipeline runtime ⊥ 算子内部的引擎后端

这是最容易混的一处,先钉死。seekbase 里有**两层**都叫得上「引擎」,但它们**不在一个层级**:

| | **pipeline runtime(本文)** | **引擎后端(算子内部)** |
|---|---|---|
| 是什么 | 承载**整条管道**执行的运行时 | 藏在**某一个算子**背后的实现 |
| 例子 | DuckDB `WITH` / bash pipeline(`run` 是两者的兜底,非独立 runtime,§2) | `search` 的 LanceDB / DuckDB-vss([search.md](search.md)) |
| 谁选它 | **编译器**(宿主指派,§4) | **算子作者**(注册哪个后端实例,operator-plugin §10.2) |
| 可扩展性 | 开放集,加一个新 runtime 见 §10 | 开放集,加一个新召回后端见 search.md |
| 对 `_in` 的关系 | `_in` 在它里面**流动**(CTE 名 / stdin) | 它**产出** `_in`,不承载下游 |

- **LanceDB 不是 runtime**。它是 `search` 这**一个算子**的召回后端;`search` 把它的结果**吐成 `_in`** 之后,就回到 runtime 的世界了。所以 §6.1 里 `search.optimize_duck` 走 DuckDB×LanceDB 官方集成、`search.optimize_bash` 调 LanceDB SDK——**LanceDB 在两个 runtime 里都出现,但它始终是 search 的内部实现,从不是宿主**。换掉 LanceDB(比如换成 duck-vss)只动 `search` 一个算子;换掉 runtime 动的是整条管道怎么编译。
- **为什么当初的「引擎可插拔」和现在的「runtime」不打架**:pipeline-as-anything §8 讲的是**前者**(search 背后引擎可换),本文讲的是**后者**(整条管道落到哪个 runtime)。一个是算子内部的纵深,一个是管道外壳的横向宿主——两根正交的可扩展轴。

## 2. 三种降级手段 = 一条代价阶梯

同一段非本宿主的算子,有三种落地方式,**代价差很多**:

| 手段 | 何时可用 | 物化 | 优化器可见 | marshal | 成本 |
|---|---|---|---|---|---|
| **① 同 runtime 原生降级** | 算子覆写了当前 runtime 的 `optimize_*` | 无 | ✅ 全可见 | 无 | **0** |
| **② 切段** | runtime 分布本来就是连续的几段 | 每个切点一次**全量** | 段内可见 | 一次 / 切点 | 中 |
| **③ 内联桥** | **永远可用**(算子总有 `run_duck`/`run_bash` 兜底) | 无(流式) | ❌ 是屏障 | **每批** | 中~高(随行数) |

- **③ 把「非当前 runtime 的一段」嵌进当前 runtime**,不切走。每个 runtime 有自己的内联形式:
  - **duck runtime 里 = 包成 vtab**(table function):DuckDB 把行喂进那段、收回结果。被嵌的可以是一条 bash 命令(`bash_vtab('jq …')`,来自 `optimize_bash`),也可以是算子的 `run_duck` 回调。
  - **bash runtime 里 = 包成读 stdin 的子进程**:那段从 **stdin** 收行、吐 stdout,当管道里一个普通命令。被嵌的可以是一段 duck SQL(`duckdb -c …`,来自 `optimize_duck`),也可以是算子的 `run_bash` 进程。
  - **`run_duck`/`run_bash` 是这条的兜底**:算子四格里≥1 非空(operator-plugin §2),任一格都能被桥进任一 runtime(带 relation↔字节 coercion),所以③**永远架得起来**。这就是为什么③标「永远可用」。
- **① 和 ② 存在的意义就是少用 ③。** 这是本文全部优化的主线。
- **② 和 ③ 不是谁绝对好**:切段付**一次全量物化**(内存 + 要求有界),内联桥付**每批 marshal** 但流式不物化。行数小且有界 → ②(更简单、两侧各自全优化);行数大或无界 → ③。

## 3. 编译流程:四个 pass

```
parse        →  段序列 s1..sn(首 token 命中 registry = 算子,否则 SQL,见 pipeline §6)
候选 runtime  →  H(si) ⊆ {duck, bash, …}:由 si 覆写了哪些 optimize_<runtime> 决定;都没覆写 = 只能靠 `run`
runtime 指派  →  给每段选一个 runtime,使总切换成本最小(§4)
融合 + codegen →  同 runtime 的连续段合并;没有当前 runtime 原生降级的段按 ③ 用 `run` 包(§5)
```

> **「首段决定宿主」是这套流程在常见情况下的退化形式**:当首段只有一个候选宿主(比如 source 只实现了一边)、且后续段都跟得上时,最小切换解自然就是「全跟着头走」。但它**不是硬规则**——一个两边都能实现的首段,不该把整条管道钉死在更贵的那一侧。

## 4. Pass 3:宿主指派 = 一条最短路

每段有候选宿主集 `H(si)`;相邻两段**同宿主免费**、**不同宿主付一次切换成本**。目标:最小化总切换成本。段数是个位数,DP / 穷举都无所谓。

```
段:      search      grep       SELECT      sh 'jq …'    SELECT
H(si):  {duck,bash} {duck,bash}  {duck}      {bash}      {duck}
                                    ↑ SQL 段只能在 duck    ↑ 只有 optimize_bash

最小切换解:  duck   →  duck   →   duck    →   bash    →  duck      = 2 次切换
```

- **成本模型先用常数**(每次切换 = 1),够用;以后可接基数估计,让「在 100 行处切」比「在 100 万行处切」便宜。
- **只有 `run_*` 的段**(没覆写任何 `optimize_*`)不挑 runtime:落在哪个 runtime 就以 ③ 内联桥承载(§7)。

## 5. Pass 4:融合与切段(三个例子)

**(a) 一个外来段夹在中间 → vtab,保持一条 SQL**

```
search cards "…" | grep ERROR | SELECT … | sh 'jq …' | SELECT …
指派:   duck        duck        duck        bash        duck
```
`jq` 用 ③ 包成 vtab,整条**仍是一条 DuckDB SQL**:
```sql
WITH s0 AS (SELECT * FROM lance_search('cards','…')),      -- search.optimize_duck
     s1 AS (SELECT * FROM s0 WHERE regexp_matches(…)),      -- grep.optimize_duck ← 关键:没用 vtab
     s2 AS (SELECT … FROM s1),
     s3 AS (SELECT * FROM bash_vtab('jq …', TABLE s2))      -- ③ 唯一的桥
SELECT … FROM s3
```

**(b) 宿主分布不交叉 → 干净切两段,零 vtab**

```
search cards "…" | grep ERROR | sh 'jq …' | sh 'sort -u'
指派:   duck        duck         bash        bash          = 1 次切换
```
两段独立:先一条 DuckDB SQL(`WITH s0, s1`),物化一次,再一条 bash pipeline(`jq … | sort -u`)。**没有 vtab**,两侧各自全优化——这就是你说的「互相不交叉就独立成段」。

**(c) `grep` 有 `optimize_duck` 省掉的那次 vtab**

```
search … | grep ERROR | SELECT … | SELECT …
```
- `grep` **只有** `optimize_bash` → 指派成 `duck,bash,duck,duck`,2 次切换,中间要架一座 vtab 桥。
- `grep` **有** `optimize_duck`(把 grep 能力整个翻译成 `WHERE regexp_matches(...)`)→ 全段 `duck`,**0 次切换、0 座桥**,而且 DuckDB 优化器能把这个 `WHERE` 和上下游一起优化(甚至下推进 `search` 的表函数)。

> 这就是**为什么值得给一个算子写两版 optimize_**:不是为了「能在两边跑」,是为了**让它不成为切换点**。一堆 duck 段中间夹一个只会 bash 的 `grep`,代价是整条管道被劈开;给它一版 `optimize_duck`,代价归零。

## 6. 算子的可选加速:两个 optimize_*

```python
class Grep(Operator):
    name = "grep"; caps = {Cap.PURE}                        # 无 accepts/emits(格式=runtime 介质)

    def _regex(self, args): return args.pattern              # 几格共用一份参数逻辑

    def optimize_duck(self, prev, args):        # duck runtime:整个翻译成 WHERE
        return f"SELECT * FROM {prev} WHERE regexp_matches({args.field}, {self._regex(args)!r})"

    def optimize_bash(self, args):              # bash runtime:就是 grep 本身
        return ["grep", "-E", self._regex(args)]
    # grep 两 runtime 都能原生 ⇒ run_duck/run_bash 都不用写
```

四格(`{optimize,run}×{duck,bash}`)**都可选**,但覆写得越多、编译器可挑的越省(**由框架检测覆写得出,不用声明**):

| 落进某 runtime 的一段 | 用哪格 | 成本 |
|---|---|---|
| 有该 runtime 的 `optimize_*` | 原生融入 | **0** |
| 只有该 runtime 的 `run_*` | 物化(vtab / 进程) | 屏障 + 每批 marshal |
| 只有另一 runtime 的实现 | ③ 内联桥过去 + relation↔字节 coercion | 屏障 + marshal + 转码 |

### 6.1 `search` 的两版(两条都是真实的实现路径)

| | `optimize_bash` | `optimize_duck` |
|---|---|---|
| 怎么实现 | 一个小命令,**直接调 LanceDB SDK** 查询,结果吐到 stdout | 用 **DuckDB × LanceDB 官方集成**,在 DuckDB 里直接查 |
| 长什么样 | `seekbase-search cards '…' --k 100` | `FROM lance_search('cards', '…', k := 100)` |
| 落在哪 | bash 宿主的管道头 | `WITH` 链的第一个 CTE |
| 好在哪 | bash 侧不必为了检索绕回 duck | 检索**进了优化器视野**:下游 `WHERE kind='issue'` / `LIMIT` 有机会下推进检索 |

**两版都有 ⇒ `search` 不挑宿主**,跟着管道其余部分走。`optimize_duck` 那版尤其值:它把 [search.md §6](search.md) 的 over-fetch ×2 从「盲目多取一倍」变成「优化器知道下游要多少」。

> **注意这张表里 LanceDB 出现了两次,但它不是 runtime**(§1.1)。两栏是**同一个 LanceDB 后端**在两个不同 runtime 里的两种接法:bash runtime 里当子进程调 SDK,duck runtime 里当表函数走官方集成。runtime 是 duck / bash,LanceDB 始终是 `search` 的内部后端。

## 7. 内联桥怎么架(vtab / stdin 两种形式)

内联桥(§2 的③)有两种形式,取决于当前 runtime;被嵌的既可以是**另一 runtime 的原生代码**(来自 `optimize_*`),也可以是算子的**物化 `run_duck`/`run_bash`**:

- **duck runtime 里 = vtab**:注册一个 **table in-out function**——吃上游关系当表参数,按批把行序列化进那段(Arrow IPC 优先,JSONL 退化),从它那儿解析回批。DuckDB 侧看到的是普通表函数,`FROM bash_vtab('jq …', TABLE s2)`。嵌 bash 命令时它 fork 子进程;嵌 `run_duck` 时批回调就是 `run_duck` 本身。
- **bash runtime 里 = 读 stdin 的子进程**:被嵌的那段从 **stdin** 收行、吐 stdout,当管道里一个普通命令。嵌 duck SQL 时是 `duckdb -c "COPY (SELECT … FROM read_json_auto('/dev/stdin')) TO '/dev/stdout' (FORMAT JSON)"`(DuckDB CLI 本就能读写 `/dev/stdin`/`/dev/stdout`);嵌 `run_bash` 时就是它那个自己 `ctx.spawn` 起子进程的 Python 进程。
- **只有 `run_*` 的算子是退化情形**:它没有任何 `optimize_*`,所以落在哪个 runtime 就用那个 runtime 的内联形式承载(vtab 或 stdin 子进程)——这也是为什么③永远架得起来。

## 8. 语义等价是作者的责任(必须有 differential test)

一个算子的两个 optimize_* **必须产生相同结果**,否则同一条 query 因为编译器选了不同宿主而**结果不同**——这是这套设计最危险的一类 bug,而且极难查。

- `grep` 的正则:`regexp_matches`(RE2)和 `grep -E`(POSIX ERE)**语义不完全一样**——反向引用、lookahead、字符类、大小写/locale 都有差。要么在文档里钉死支持的子集,要么在编译期检测到「用了不可移植的语法」时**锁定宿主**。
- **强制 differential test**:同一份输入,分别强制走 duck 版和 bash 版,断言逐行一致。这是双 optimize_* 算子的**准入条件**,不是可选测试。
- 编译器提供 `EXPLAIN`:打印宿主指派 + 每个切点用了 ②/③,否则用户无法解释「为什么这条快那条慢」。

## 9. 有界性塌缩成宿主属性

Flink 那套 `bounded` 传播机制,在这个后端下**根本不用实现**——有界性是宿主自带的性质([operator-plugin.md §3.3](operator-plugin.md)):

| 宿主 | 有界性 | 说明 |
|---|---|---|
| DuckDB `WITH` | **必然有界** | `FROM` 需要有限关系,物理约束 |
| bash pipeline | **可无界** | `tail -f \| grep …` 是内核管道的日常 |

于是只剩**一条**规则要检查(其余的传播机械可以删):

```
sh 'tail -f app.log' | SELECT count(*) FROM _in
└─ bash 宿主,无界 ────┴─ duck 宿主 ⇒ 编译期报错(无界流不能进 duck)
```

价值不变:**把一类「跑起来才发现永远不返回」变成编译期错误**;成本降了——不用发明传播算法,只用问「这一段落在哪个宿主」。

## 10. 加一个新 runtime:扩展契约

runtime 是**开放集**——`{duck, bash}` 是当下这两个(`run` 是两者的兜底、不是第三个 runtime),不是硬编码的上限。为什么会想加?比如 **WASM/沙箱 runtime**(在隔离环境跑不可信算子)、**远程 runtime**(把段推到另一台机 / 一个 serverless)、**GPU/向量 runtime**(专为 embedding 批算)。加一个 runtime `R`,要给编译器四样东西:

| 契约 | 是什么 | duck 的例子 | bash 的例子 |
|---|---|---|---|
| **① codegen 目标** | `R` 认什么代码;算子覆写 `optimize_R(...)` 返回它 | 一个 CTE 体(SQL 文本) | 一段 argv |
| **② 拼接算子** | 同 `R` 的相邻段怎么合成一个执行单元 | 串成 `WITH` 链 | 用 `\|` 串成 pipeline |
| **③ 到别的 runtime 的桥** | `R` ↔ 其它 runtime 怎么 marshal `_in`(§7) | vtab 表函数 / `COPY … /dev/stdout` | stdin/stdout + Arrow/JSONL |
| **④ 有界性** | `R` 承载的流有界还是可无界(§9) | 必然有界 | 可无界 |

给齐这四样,`R` 就是编译器眼里的一等宿主:宿主指派(§4)的候选集自动多一个 `R`,代价阶梯(§2)对它照常适用,算子只要多覆写一个 `optimize_R` 就能免费落进去。**编译器的三个 pass(指派 / 融合 / 桥)对 runtime 数量无感**——最短路在 `{duck, bash, R, …}` 上照跑,融合按「同 runtime」聚合,桥查②③的表。

- **加 runtime 不用改算子**:没覆写 `optimize_R` 的老算子,在 `R` 里就靠 `run` 内联承载(③)——和今天 `run`-only 算子的待遇一样。**新 runtime 不破坏既有算子**,只是给愿意覆写 `optimize_R` 的算子一条新的免费路。
- **`optimize_<runtime>` 的命名不是偶然**:方法名里嵌 runtime 名,正是为了让「加 runtime = 加一族 `optimize_R` 覆写」成为纯扩展,基类不用动(operator-plugin §3.2 的方法是开放集,不是固定两个)。
- **桥是 O(runtime²) 的账**:严格说每对 runtime 都要一座桥。实践上**都经 `_in` 的标准序列化**(Arrow IPC / JSONL)中转,所以每个新 runtime 只需实现「与标准格式互转」一次(读/写 Arrow),不用为每个已有 runtime 各写一座——O(runtime²) 塌成 O(runtime)。这也是为什么 `_in` 恒定一种 ABI(pipeline §2)在这里第二次付红利。

> 边界:**加 runtime 是加执行宿主,不是加查询语言。** 新 runtime 仍然只吃/吐 `_in`、仍然受 operator-registry 的能力/策略约束(远程/EXEC 类默认更严)。它扩的是「这段在哪跑」,不扩「能表达什么」——后者的天花板见 §11 第一条。

## 11. 诚实的代价 / 边界

- **我们被所有 runtime 的能力上限的并集锁死。** 不自己造运行时的代价就是:没有任何一个 runtime 能表达的东西,seekbase 也表达不了。这是**故意的**——换来一批成熟的优化器/调度器,而不是一个自研的半成品。加 runtime(§10)能抬高这个天花板,但每个新 runtime 也带来它自己的运维/安全账。
- **vtab 是优化屏障**:桥两侧 DuckDB 看不穿,谓词推不进去、基数估计失真,优化器可能因此选坏计划。所以 ③ 是保底不是常态,§5(c) 那种「多写一个 optimize_* 消掉桥」的收益是复利的。
- **双 optimize_* = 双份维护 + 等价性风险**(§8)。只写一版是完全正当的选择;它只是意味着这个算子**是个切换点**,请在文档里说清楚。
- **成本模型是假的**:常数切换成本会在「小表处切 vs 大表处切」上选错。要真优化得接基数估计——而基数只有 DuckDB 侧知道,bash 侧无从估计,**这是这套模型固有的盲区**。
- **可解释性变差**:用户写的是 5 段管道,跑的是 1 条 SQL + 1 个子进程。报错信息、行号、性能归因都要能映射回用户写的那一段,否则不可用(所以 §8 的 `EXPLAIN` 是必需品不是奢侈品)。
- **bash 宿主 = `EXEC` 能力**:整条管道降级成 bash pipeline 意味着起子进程,[operator-registry.md](operator-registry.md) 的策略/沙箱**照常适用**——默认 `read-only` 下 bash 宿主直接不可用,不因为「它只是个编译目标」而放松。

## 12. 与其他文档

- [pipeline-as-anything.md](pipeline-as-anything.md):前端语法与 `_in` ABI;§2.1「接缝才切」在这里得到可计算的定义(**真接缝 = runtime 切换点**)。
- [operator-plugin.md](operator-plugin.md):算子契约——两轴四方法(原生 `optimize_<runtime>` × 物化 `run_<runtime>`)、无 `accepts`/`emits`(格式=runtime 介质);§3.3 的有界性按本文 §9 简化。
- [operator-registry.md](operator-registry.md):bash 宿主 = `EXEC`,能力/策略/沙箱不因编译降级而放松。
- [search.md](search.md):`search` 的两个 optimize_*(LanceDB SDK / DuckDB×LanceDB 官方集成),以及 `optimize_duck` 版让 over-fetch 进入优化器视野。
- [architecture.md](architecture.md):`PipelineService` = 本文这个编译器,不是执行器。
- [pipeline-streaming.md](pipeline-streaming.md):无界 source ⇒ 只能 bash + 常驻,是本文有界性规则(§9)+ runtime 指派的推论;bash 管道当简易流框架。
