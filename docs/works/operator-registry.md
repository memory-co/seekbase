# operator-registry — 万物皆算子:注册机制 + 权限范围(Claude / Codex 式)

> 状态:**已落**(`operator/registry.py` 注册解析 + `operator/policy.py` 能力×策略:deny > allow > 模式缺省,编译期拒;沙箱边界 = scratch cwd + 最小 env + 进程组 + 墙钟超时——网络隔离不在进程内强制,`ask` 交互态延后)。管道里每一段的 verb(`search` / `grep` / `find` / `sed` / `sh` / `http` / `embed` …)都是一个**注册算子**。**`search` 不特殊——它只是一个注册算子、一条最佳实践**;`find`/`sed`/`grep` 是另外几条最佳实践,和 `search` 平级地注册进同一张表。本文定两件事:① 算子怎么注册(契约 + registry);② 算子的**使用范围怎么限**——像 Claude Code / Codex 那样按**能力(capability)+ 策略(policy)**授权,给 pipeline-as-anything §9「算子段是安全洞」一个正式的围栏。
>
> 依赖:[pipeline-as-anything.md](pipeline-as-anything.md)(管道模型、`_in` 表 ABI、§2.1「接缝才切」、§9 的安全担忧)。

## 1. 定位:`search` 只是众多注册算子之一

pipeline-as-anything 把 query 拆成 `stage | stage`,每段吃一张表、吐一张表。**每个 stage 的 verb 都由 registry 解析成一个算子**——`search` 也不例外。它在旧设计里是「一等算子」,在这里**降级成一条最佳实践**:一个 **source**(方法不吃上游)、产 `(pk,_score,…)` 表的注册算子。

```
                         Operator Registry(一张表)
   ┌──────────┬──────────┬──────────┬──────────┬──────────┬──────────┐
  search      scan       grep       find       sed        sh / http
 (source)   (source)  (operator) (operator) (operator) (operator)
 向量检索    读业务表    行过滤     列文件      流编辑     shell / 网络
   └──────── 都实现同一个「表进表出」契约,都受同一套权限策略约束 ────────┘
```

- **没有特权算子**:`search` 和 `grep` 在 registry 里是同一种公民,靠 **`caps` + 覆写了哪几格执行方法**(§3、[operator-plugin.md §3.2](operator-plugin.md))区分,不靠硬编码。想把检索换成别的召回策略?注册一个新的 source 算子(方法不吃上游)即可,不动编译器。
- **最佳实践 = 一组预置算子**:seekbase 自带一批(§4);它们是「推荐这么用」的沉淀,不是语言内建。用户可注册自己的算子(§5)。

## 2. 一次注册记录:算子的契约

一个算子就是一个 **`Operator` 子类**——**名字 + 参数签名 + 能力 + 执行方法**(无格式声明,格式是 runtime 介质):

```python
class Grep(Operator):
    name = "grep"                           # 无 accepts/emits(格式=runtime 介质)
    caps = {Cap.PURE}                       # 声明它碰什么外界资源(§3)——授权判据

    class Args(ArgSpec):                    # 参数签名:供解析 + --help
        pattern: str
        field:   str | None = None

    def optimize_duck(self, prev, args): ...   # 原生:一段 SQL,0 成本
    def optimize_bash(self, args):       ...   # 原生:一条 argv
    # run_duck / run_bash:物化兜底(有屏障+ctx),grep 靠两个 optimize 就够,不用写
```

registry 存的就是这些类(或实例)。**完整的作者契约**——四格执行矩阵(`{optimize,run}×{duck,bash}`)、服务型怎么写、为什么必须是类而不是一条记录——见 [operator-plugin.md](operator-plugin.md)。

- **不用 `accepts`/`emits`、不用 `kind`**:格式是 runtime 的介质(duck=table、bash=字节流),不声明;是不是 source(不吃上游)/sink(无下游)由执行方法的**签名**推导。组合永远合法(同 runtime 直接接、跨 runtime 框架转一次),没有「格式不匹配」这种编译期错误([operator-plugin.md §4/§8](operator-plugin.md))。
- **`caps` 是授权的唯一判据**:算子**必须诚实声明**自己碰哪些外界资源(读文件?写文件?联网?起子进程?)。策略层(§6)只看 `caps`,不猜实现干了啥——**声明不实 = 安全漏洞**,所以预置算子的 caps 是审过的,用户算子注册时也要显式写。
- **`run_duck` / `run_bash` 收 `ctx`**:上下文里带**已授的权限边界**(可读哪些路径、能否联网、沙箱句柄),算子只能在边界内动手——纵深防御,不只靠编译期检查(`run_bash` 起子进程走 `ctx.spawn`)。而 `optimize_*` **拿不到 `ctx`**:它们只生成代码,真正的资源访问发生在代码跑起来时,由沙箱管(§6.3)。

## 3. 能力(capability):算子碰什么外界资源

授权不按算子名逐个开,而按**能力**分级——这是 Claude Code「工具按类授权」、Codex「沙箱按资源限」的同一思路:

| 能力 | 含义 | 例子算子 | 危险度 |
|---|---|---|---|
| `PURE` | 纯计算,只碰 `_in`,不碰外界 | `grep` `sed` `sort`(表内) | 无 |
| `FS_READ` | 读文件系统(限定根) | `find` `read <file>` `grep <path>` | 低 |
| `FS_WRITE` | 写文件系统 | `write <file>` `sed -i` | 中 |
| `NET` | 联网 | `http` `embed`(API embedder) | 中 |
| `EXEC` | 起**任意**子进程 | `sh '<任意命令>'` | **高** |

- **一个算子可声明多能力**:`grep <path>` = `FS_READ`;纯表内 `grep` = `PURE`。同名算子按参数落到不同能力(覆写 `parse_args` 自报,见 [operator-plugin.md §6](operator-plugin.md))。
- **`EXEC` 是特殊的**:`sh` 能跑任意命令 = 把上面所有能力一次性放开,所以它单列最高危,默认最严(§6)。**能用专用算子就别用 `sh`**——`grep`/`find`/`sed` 存在的意义之一,就是把常见 shell 活收成 `PURE`/`FS_READ` 的窄能力算子,不必动用 `EXEC`。

## 4. 预置的最佳实践算子(自带一批)

seekbase 预置一批审过 caps 的算子。**`search` 只是其中一个**:

| 算子 | 位置 | caps | 干什么 |
|---|---|---|---|
| `search <表> '文本'` | source | `PURE`(引擎内) | 向量+全文 hybrid 检索,产 `(pk,_score,…)`(见 [search.md](search.md)) |
| `scan <表> [@asof]` | source | `PURE` | 读业务表的可见性视图(时光机入口,见 [time_machine.md](time_machine.md)) |
| `read <file>` | source | `FS_READ` | 读文件成表(`read_json_auto`/csv) |
| `grep <pat>` | 中间 | `PURE` / `FS_READ` | 按正则过滤 `_in` 的行,或 grep 文件成表 |
| `find <expr>` | source | `FS_READ` | 列文件成表(名字/大小/时间) |
| `sed <script>` | 中间 | `PURE` | 对 `_in` 的文本列做流式改写 |
| `http <url>` | 中间 | `NET` | 拿 `_in` 当参数打 HTTP,响应解析回表 |
| `embed <col>` | 中间 | `NET` | 给 `_in` 某列补向量列 |
| `jq '<script>'` | 中间 | `EXEC` | 外部 CLI(`ExternalCommand`,只 `optimize_bash`);读 stdin 的 JSONL 是它内部的事 |
| `sh '<cmd>'` | 中间 | `EXEC` | 逃生舱:任意 shell,`_in` 过 stdin、stdout 回表 |

- **`search` vs `grep`/`find`**:都是召回/过滤的最佳实践,只是**检索维度不同**——`search` 是语义/BM25 召回,`grep` 是正则精确匹配,`find` 是文件系统枚举。管道里可以串:`find data/ -name '*.log' | grep 'ERROR' | search ... `——**每一段都是一个注册算子**。
- **SQL 段不进 registry**:首 token 不命中算子名的段,就是一条原生 DuckDB SQL(over `_in`,pipeline §2.1)——它是**缺省**,不是「算子」。registry 只管**首 token 命中、跳出 SQL** 的 verb。

## 5. registry 怎么解析(编译期)

管道解析(pipeline §6)时,每段只看**首 token**去 registry 查——**SQL 是缺省,命中才走算子**:

```
parse("search cards '…' | SELECT … | sh 'jq …'")
  ├─ "search" → registry 命中 (source, PURE)   → 走 search 算子 ✓
  ├─ "SELECT" → registry 未命中                → 整段当 DuckDB SQL(缺省)
  └─ "sh"     → registry 命中 (中间, EXEC)     → 走 sh 算子,caps 交策略层判(§6)
```

- **首 token 不命中 registry → 这段就是 SQL,不是错误**。SQL 是一等公民、也是 fallback(pipeline §6):registry 只在首 token 匹配算子名时才接管;`SELECT …`/`WITH …` 的引导关键字天然不在 registry 里,照走 DuckDB。**所以「未知算子」不存在**——不匹配即 SQL。
- **命名不撞**:算子名不取 SQL 引导关键字(`select`/`with`/`from`…),从根上避免「本想写 SQL 却被当成算子」的歧义。
- **内建 + 用户注册同一张表**:`open(..., operators=[MyOperator(...)])` 把自定义算子挂进 registry;名字冲突显式报错(不覆盖内建)。用户算子**必须声明 caps**,进不了「审过」名单、默认按声明的 caps 受策略约束。**怎么实现一个 `MyOperator`**(四格执行矩阵、`run_*`/`optimize_*` 签名、声明 schema)见 [operator-plugin.md](operator-plugin.md)。

## 6. 权限范围:能力 × 策略(Claude / Codex 式)

**核心**:一段管道能不能跑某算子,不看算子名,看它的 `caps` 是否落在当前**策略(policy)**允许的范围内。策略借鉴两处成熟设计:

- **Claude Code**:工具按类 **allow / ask / deny** 三态,权限模式(`default` / `acceptEdits` / `plan` / `bypassPermissions`)决定升级时问不问。
- **Codex**:`EXEC` 类算子跑在**沙箱**里——限定可写目录、禁网、资源上限(approval modes:`read-only` / `workspace-write` / `danger-full-access`)。

seekbase 的策略把这两套并起来:

### 6.1 权限模式(一次 query / 一个 session 的默认范围)

| 模式 | 允许的 caps | 语义 | 类比 |
|---|---|---|---|
| `read-only`(默认) | `PURE`, `FS_READ`, `NET`? | 只读:检索/过滤/读文件;**拒 `FS_WRITE`/`EXEC`** | Codex read-only / Claude plan |
| `sandboxed` | 上 + `EXEC`(**在沙箱内**) | 允许 shell,但限定工作目录、禁网、资源上限 | Codex workspace-write |
| `trusted` | 全部 | 显式开、放开一切(本机可信调用方) | Claude bypassPermissions |

- **默认 `read-only`**:query 是数据接口,默认**不该**能写盘 / 起任意进程。`sh` 这类 `EXEC` 算子**默认不可用**——这就是 pipeline §9「算子段 = RCE 风险」的正式答案:默认关,开要显式升级。
- **`NET` 是可选项**:纯本地 memory 部署可以连 `NET` 一起关(`read-only` 去掉 `NET` → 连 `http`/`embed` 都禁),看部署面。

### 6.2 三态决策(allow / ask / deny)+ 逐算子覆盖

模式给的是**默认范围**,再叠一层显式覆盖(allowlist / denylist),按算子或按能力:

```
policy = {
  mode: "read-only",
  allow: ["search", "scan", "grep", "find"],   # 白名单:只这些能跑(更严)
  deny:  ["sh"],                               # 黑名单:永不(即便升级)
  ask:   ["http"],                             # 灰:每次问(交互式确认)
}
```

- **决策顺序**:`deny` > `allow` > 模式默认。命中 `deny` → 直接 `PermissionDenied`(早失败,编译期就拒,管道不启动);命中 `ask` → 交互式确认(HTTP 形态下 = 一个待确认响应);否则按模式 caps 判。
- **能力级 deny**:`deny_caps: [EXEC, FS_WRITE]` 一句话封掉所有该类算子,不用逐个列名——按类授权比按名授权更抗「新算子漏配」。

### 6.3 沙箱:`EXEC` 算子的执行边界(纵深防御)

即使策略放行了 `EXEC`(`sandboxed`/`trusted`),`sh` 的子进程仍在**沙箱**里跑,`ctx` 带着边界:

- **文件系统**:只可读授予的根、只可写临时工作目录(Codex workspace 式);越界的路径访问在 `ctx` 层就拒。
- **网络**:`sandboxed` 默认禁网(除非算子另有 `NET` 授权)。
- **资源**:CPU / 内存 / 墙钟上限,超时 kill——一段 `sh` 卡死不能拖垮整个 query 端口。
- **为什么编译期检查还不够**:声明的 caps 可能不实、算子实现可能有 bug——沙箱是**第二道墙**,把「算子越权」的爆炸半径限死在子进程里。

## 7. 一次带算子的管道,权限怎么串起来

```
policy = read-only + allow[search,grep] + deny[sh]

search cards 'pty 终端'        → registry:source PURE   → allow(在白名单) ✓
  | SELECT * FROM _in WHERE …  → transform(原生 SQL)    → 不过 registry ✓
  | grep 'ERROR' --field issue → registry:算子 PURE     → allow(在白名单) ✓
  | sh 'curl …'                → registry:算子 EXEC      → DENY(命中 deny) ✗ 编译期拒,管道不启动
```

- 前三段通过:都是 `PURE`、都在白名单。第四段 `sh` 命中 `deny` → 整条管道**编译期就被拒**,一步都不执行(fail-closed)。
- 换 `trusted` 模式且不 deny `sh` → 第四段放行,但 `curl` 仍在沙箱里(§6.3),禁网策略下照样连不出去。

## 8. 诚实的代价 / 边界

- **caps 声明的可信度是地基**:整套授权建立在「算子诚实声明 caps」上。预置算子审过;用户算子靠沙箱(§6.3)兜底,但一个声明成 `PURE` 却偷偷起进程的恶意算子,只有沙箱能拦——所以 `EXEC`/`FS_WRITE` 默认沙箱,不信声明。
- **默认严 = 开箱少能力**:`read-only` 默认关掉 `sh`/写盘,很多「顺手串个脚本」的用法要显式升级模式。这是有意的——**数据接口默认不该是 RCE**;方便性用显式授权换,不用默认放开换。
- **不是完整的多租户 ACL**:这里管的是「一次 query 能调哪些算子、算子能碰哪些资源」,**不**管「用户 A 能不能看表 T 的行」(那是行级授权,另一层,见 DESIGN 待定)。算子权限 ⊥ 数据权限。
- **沙箱依赖平台**:资源上限 / 网络隔离的强度随 OS 能力变(cgroups / seccomp / 容器)。弱平台上沙箱退化成「尽力而为」,此时更该靠 `deny` 名单把 `EXEC` 直接关死。

## 9. 与其他文档

- [operator-plugin.md](operator-plugin.md):**作者视角**——要写一个算子,plugin 契约(`run(in_table,args,ctx)`)、三种作者形态、如何声明 caps/输出 schema(本文是系统视角,那篇是实现侧)。
- [pipeline-as-anything.md](pipeline-as-anything.md):管道模型、`_in` 表 ABI、§2.1「接缝才切」、§9 算子段安全担忧(本文是它的答案)。
- [search.md](search.md):`search` 作为一个 source 算子(可插拔引擎),只是众多注册算子里的一个。
- [time_machine.md](time_machine.md):`scan @asof` source 算子的 as-of 语义。
- [architecture.md](architecture.md):`PipelineService` 编译期查 registry + 判策略的位置。
