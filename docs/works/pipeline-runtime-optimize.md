# pipeline-runtime-optimize — 管道不自己跑:降级到 DuckDB `WITH` / bash pipeline,以及怎么少花钱

> 状态:**设计稿(pipeline 方向,未落)**。[pipeline-as-anything.md](pipeline-as-anything.md) 定的是**前端**(query = `stage | stage`);本文定**后端**:这根管道**不由 seekbase 自己执行**,而是被**降级(lowering)**成两个现成运行时之一——**DuckDB 的 `WITH` 链**或 **bash 的 pipeline**。我们不造管道运行时,我们造编译器。
>
> 于是「优化」有了精确含义:**同一条 query 有多种降级方案,成本差一个数量级,编译器要挑便宜的那个。** 本文就是这套挑法。
>
> 依赖:[pipeline-as-anything.md](pipeline-as-anything.md)(前端语法、`_in` ABI、§2.1「接缝才切」)、[tool-plugin.md](tool-plugin.md)(算子契约:`nativeDuckdb` / `nativeBash` / 保底 `run`)。

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

- **管道里的 `|` 大多数会被编译掉**。同宿主的相邻段之间,`|` 只是语法——落到 `WITH` 里是一个 CTE 边界(DuckDB 优化器**看穿**它),落到 bash 里是一个真管道符(内核给背压)。
- **只有跨宿主的 `|` 是真接缝**。这给了 pipeline §2.1「接缝才切」一个可计算的定义:**真接缝 = 宿主切换点**,数量由编译器算出来,不由用户写出来。
- **两个宿主各自带来白送的东西**:DuckDB `WITH` = 跨段谓词下推 / join 重排 / 基数估计;bash pipeline = 背压、增量、无界流。**这正是不自己造运行时的理由**——自己造的话这两样都得重写一遍,还写不过它们。

## 2. 三种降级手段 = 一条代价阶梯

同一段非本宿主的算子,有三种落地方式,**代价差很多**:

| 手段 | 何时可用 | 物化 | 优化器可见 | marshal | 成本 |
|---|---|---|---|---|---|
| **① 同宿主原生降级** | 算子实现了当前宿主的 `native*` | 无 | ✅ 全可见 | 无 | **0** |
| **② 切段** | 宿主分布本来就是连续的几段 | 每个切点一次**全量** | 段内可见 | 一次 / 切点 | 中 |
| **③ vtab 桥** | **永远可用(保底)** | 无(流式) | ❌ 桥是屏障 | **每批** | 中~高(随行数) |

- **③ 是保底的通用桥**:duck 宿主里遇到 bash 命令,把它的输入输出包成 **vtab(表函数)**,DuckDB 照样能用它——**一定跑得通,但一定加开销**。
- **① 和 ② 存在的意义就是少用 ③。** 这是本文全部优化的主线。
- **② 和 ③ 不是谁绝对好**:切段付**一次全量物化**(内存 + 要求有界),vtab 付**每批 marshal** 但流式不物化。行数小且有界 → ②(更简单、两侧各自全优化);行数大或无界 → ③。

## 3. 编译流程:四个 pass

```
parse        →  段序列 s1..sn(首 token 命中 registry = 算子,否则 SQL,见 pipeline §6)
候选宿主      →  H(si) ⊆ {duck, bash}:由 si 实现了哪些 native* 决定;都没实现 = {vtab-only}
宿主指派      →  给每段选一个宿主,使总切换成本最小(§4)
融合 + codegen →  同宿主的连续段合并;跨宿主处按 ②/③ 落地(§5)
```

> **「首段决定宿主」是这套流程在常见情况下的退化形式**:当首段只有一个候选宿主(比如 source 只实现了一边)、且后续段都跟得上时,最小切换解自然就是「全跟着头走」。但它**不是硬规则**——一个两边都能实现的首段,不该把整条管道钉死在更贵的那一侧。

## 4. Pass 3:宿主指派 = 一条最短路

每段有候选宿主集 `H(si)`;相邻两段**同宿主免费**、**不同宿主付一次切换成本**。目标:最小化总切换成本。段数是个位数,DP / 穷举都无所谓。

```
段:      search      grep       SELECT      sh 'jq …'    SELECT
H(si):  {duck,bash} {duck,bash}  {duck}      {bash}      {duck}
                                    ↑ SQL 段只能在 duck    ↑ 只有 nativeBash

最小切换解:  duck   →  duck   →   duck    →   bash    →  duck      = 2 次切换
```

- **成本模型先用常数**(每次切换 = 1),够用;以后可接基数估计,让「在 100 行处切」比「在 100 万行处切」便宜。
- **`{vtab-only}` 的段**(只有 Python `run`、没有任何 `native*`)不参与选择:它被钉在当前宿主上,以 ③ 落地。

## 5. Pass 4:融合与切段(三个例子)

**(a) 一个外来段夹在中间 → vtab,保持一条 SQL**

```
search cards "…" | grep ERROR | SELECT … | sh 'jq …' | SELECT …
指派:   duck        duck        duck        bash        duck
```
`jq` 用 ③ 包成 vtab,整条**仍是一条 DuckDB SQL**:
```sql
WITH s0 AS (SELECT * FROM lance_search('cards','…')),      -- search.nativeDuckdb
     s1 AS (SELECT * FROM s0 WHERE regexp_matches(…)),      -- grep.nativeDuckdb ← 关键:没用 vtab
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

**(c) `grep` 有 `nativeDuckdb` 省掉的那次 vtab**

```
search … | grep ERROR | SELECT … | SELECT …
```
- `grep` **只有** `nativeBash` → 指派成 `duck,bash,duck,duck`,2 次切换,中间要架一座 vtab 桥。
- `grep` **有** `nativeDuckdb`(把 grep 能力整个翻译成 `WHERE regexp_matches(...)`)→ 全段 `duck`,**0 次切换、0 座桥**,而且 DuckDB 优化器能把这个 `WHERE` 和上下游一起优化(甚至下推进 `search` 的表函数)。

> 这就是**为什么值得给一个算子写两版 native**:不是为了「能在两边跑」,是为了**让它不成为切换点**。一堆 duck 段中间夹一个只会 bash 的 `grep`,代价是整条管道被劈开;给它一版 `nativeDuckdb`,代价归零。

## 6. 算子要交的作业:两版 native

```python
Tool(
    name = "grep", accepts={Fmt.TABLE}, emits=Fmt.TABLE, caps={Cap.PURE},
    native_duckdb = lambda prev, a: f"SELECT * FROM {prev} WHERE regexp_matches({a.field}, {a.pat!r})",
    native_bash   = lambda a: ["grep", "-E", a.pat],
    run           = grep_run,        # 保底:两边都没有时,框架包成 vtab(③)
)
```

三格**都可选**,但填得越多、编译器可挑的越多:

| 只填 | 后果 |
|---|---|
| `run` | 永远走 ③ vtab;能跑,最贵 |
| 一版 `native*` | 在那个宿主里免费,在另一个宿主里是**切换点** |
| 两版 `native*` | **永不成为切换点**——跟着上下文走,零成本 |

### 6.1 `search` 的两版(两条都是真实的实现路径)

| | `nativeBash` | `nativeDuckdb` |
|---|---|---|
| 怎么实现 | 一个小命令,**直接调 LanceDB SDK** 查询,结果吐到 stdout | 用 **DuckDB × LanceDB 官方集成**,在 DuckDB 里直接查 |
| 长什么样 | `seekbase-search cards '…' --k 100` | `FROM lance_search('cards', '…', k := 100)` |
| 落在哪 | bash 宿主的管道头 | `WITH` 链的第一个 CTE |
| 好在哪 | bash 侧不必为了检索绕回 duck | 检索**进了优化器视野**:下游 `WHERE kind='issue'` / `LIMIT` 有机会下推进检索 |

**两版都有 ⇒ `search` 不挑宿主**,跟着管道其余部分走。`nativeDuckdb` 那版尤其值:它把 [search.md §4](search.md) 的 over-fetch ×2 从「盲目多取一倍」变成「优化器知道下游要多少」。

## 7. vtab 桥怎么架(两个方向)

- **duck 里插 bash**:注册一个 **table in-out function**——吃上游关系当表参数,fork 子进程,按批把行序列化进 stdin(Arrow IPC 优先,JSONL 退化),从 stdout 解析回批。DuckDB 侧看到的是普通表函数,`FROM bash_vtab('jq …', TABLE s2)`。
- **bash 里插 duck**:反过来,管道中间放一个 `duckdb -c "COPY (SELECT … FROM read_json_auto('/dev/stdin')) TO '/dev/stdout' (FORMAT JSON)"`——DuckDB CLI 本来就能读写 `/dev/stdin`/`/dev/stdout`,这一段就是普通的 bash 命令。
- **Python `run` 算子同理**:没有任何 `native*` 的算子,由框架包成 duck 侧的 vtab(批回调就是它的 `run`/`process`),或包成 bash 侧的一个子命令。

## 8. 语义等价是作者的责任(必须有 differential test)

一个算子的两版 native **必须产生相同结果**,否则同一条 query 因为编译器选了不同宿主而**结果不同**——这是这套设计最危险的一类 bug,而且极难查。

- `grep` 的正则:`regexp_matches`(RE2)和 `grep -E`(POSIX ERE)**语义不完全一样**——反向引用、lookahead、字符类、大小写/locale 都有差。要么在文档里钉死支持的子集,要么在编译期检测到「用了不可移植的语法」时**锁定宿主**。
- **强制 differential test**:同一份输入,分别强制走 duck 版和 bash 版,断言逐行一致。这是双 native 算子的**准入条件**,不是可选测试。
- 编译器提供 `EXPLAIN`:打印宿主指派 + 每个切点用了 ②/③,否则用户无法解释「为什么这条快那条慢」。

## 9. 有界性塌缩成宿主属性

[tool-plugin.md §3.3](tool-plugin.md) 那套 `bounded` 传播,在这个后端下**大部分不需要了**——有界性变成宿主自带的性质:

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

## 10. 诚实的代价 / 边界

- **我们被两个宿主的能力上限锁死。** 不自己造运行时的代价就是:DuckDB 表达不了、bash 也表达不了的东西,seekbase 也表达不了。这是**故意的**——换来两套成熟的优化器/调度器。
- **vtab 是优化屏障**:桥两侧 DuckDB 看不穿,谓词推不进去、基数估计失真,优化器可能因此选坏计划。所以 ③ 是保底不是常态,§5(c) 那种「多写一版 native 消掉桥」的收益是复利的。
- **双 native = 双份维护 + 等价性风险**(§8)。只写一版是完全正当的选择;它只是意味着这个算子**是个切换点**,请在文档里说清楚。
- **成本模型是假的**:常数切换成本会在「小表处切 vs 大表处切」上选错。要真优化得接基数估计——而基数只有 DuckDB 侧知道,bash 侧无从估计,**这是这套模型固有的盲区**。
- **可解释性变差**:用户写的是 5 段管道,跑的是 1 条 SQL + 1 个子进程。报错信息、行号、性能归因都要能映射回用户写的那一段,否则不可用(所以 §8 的 `EXPLAIN` 是必需品不是奢侈品)。
- **bash 宿主 = `EXEC` 能力**:整条管道降级成 bash pipeline 意味着起子进程,[tool-registry.md](tool-registry.md) 的策略/沙箱**照常适用**——默认 `read-only` 下 bash 宿主直接不可用,不因为「它只是个编译目标」而放松。

## 11. 与其他文档

- [pipeline-as-anything.md](pipeline-as-anything.md):前端语法与 `_in` ABI;§2.1「接缝才切」在这里得到可计算的定义(**真接缝 = 宿主切换点**)。
- [tool-plugin.md](tool-plugin.md):算子契约——`native_duckdb` / `native_bash` / 保底 `run`;§3.3 的 `bounded` 传播按本文 §9 简化。
- [tool-registry.md](tool-registry.md):bash 宿主 = `EXEC`,能力/策略/沙箱不因编译降级而放松。
- [search.md](search.md):`search` 的两版 native(LanceDB SDK / DuckDB×LanceDB 官方集成),以及 `nativeDuckdb` 版让 over-fetch 进入优化器视野。
- [architecture.md](architecture.md):`PipelineService` = 本文这个编译器,不是执行器。
