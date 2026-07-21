# seekbase

一个 supabase 风格的数据端口,**query 是一根 SPL 式管道**——`stage | stage`,**SQL 是一等公民、也是缺省**:一段首 token 命中注册算子(`search`/`scan`/`grep`)才走算子,否则整段就是一条 DuckDB SQL;纯 SQL 查询零管道、原样执行。检索是管道的一个 **source 段**(`search 表 '文本' | SELECT … FROM _in`),hybrid = 向量语义(`vss`/HNSW)+ 全文(`fts`/BM25)RRF 融合、中文 jieba 分词;整条管道**编译成一条 DuckDB `WITH` SQL** 执行,不自建运行时。再加一份本地文件镜像用于审计。一个端口、一个文件、零运维。

**两种使用形态,一套完全相同的 API:**

| 形态 | 怎么拿到 db | 跑在哪 | 什么时候用 |
|---|---|---|---|
| **嵌入(embedded)** | `Seekbase.open(dir, schema=…)` | 进程内、DuckDB | 单进程、本地优先、零运维 |
| **服务(server)** | `Seekbase.connect(url)` → 连一个运行中的 server | HTTP | 多客户端 / 多进程共享同一个实例 |

两种形态的**调用代码逐字节相同**——`query(sql)` / `insert` / `delete` 一个字都不用改,变的只有你怎么拿到 `db` 句柄。

> **状态:核心完整(管道 M1 + 单表同步写)。** 管道 `query`(SQL 缺省 + `search`/`scan`/`grep` 算子段,整条编译成一条 `WITH` SQL;duck runtime)、`ds` 时间窗、同步 ticket 写(`insert`/`delete`,**主键写一次、重复报错**)、文件镜像(每表 `<表>.jsonl` + `rebuild`)、检索后端**可插拔**(`Seekbase.open(search_backend="vss"|"lance")`)、**bash runtime**(`sh`/`jq` 段,默认 `read-only` 策略拒 EXEC、`Policy(mode="sandboxed")` 放行进沙箱)、**流式摄取**(`db.stream("watch '<glob>' | … | ingest <表>")`,at-least-once + 幂等 sink)、**统一 task 句柄**(写=出生即 done;rebuild/`as_task` 查询=后台 task;HTTP 慢查询 `wait_ms` 超时自动 202 升级,结果落文件按保留期 GC)、两种使用形态,今天都能跑。**delete 只软删 `deleted_ds` 墓碑、历史永久保留,没有物理删/vacuum**。完整设计见 [DESIGN.md](DESIGN.md)。

> **🚧 剩余优化(设计已定,未落):** ③ 内联桥/vtab(跨 runtime 现走 ② 切段 + JSONL 桥)、runtime 指派最短路(现为退化型)、常驻流中段链(现为 batch-scoped)。旧的 `search()` UDF 语法已退休。

## 安装

```bash
pip install seekbase   # 嵌入 + HTTP 客户端 + server(seekbase_server)+ ApiEmbedder,全部开箱即用
```

两种形态都是标配、无需任何 extra:嵌入、HTTP 客户端(`Seekbase.connect`)、以及把端口暴露成 HTTP 服务的 `seekbase_server(db)`——它是一个**零依赖的手写 ASGI app**。跑这个 app 的 **ASGI runner 由你从外部注入**(uvicorn / hypercorn / gunicorn,或挂进已有应用),seekbase 不绑定 runner。

## 嵌入形态(进程内)

```python
from seekbase import Seekbase

SCHEMA = [
    {
        "table": "cards",
        "columns": [
            {"name": "card_id", "type": "str"},
            {"name": "issue",   "type": "str"},
            {"name": "kind",    "type": "str"},
        ],
        "primary": "card_id",
        "searchable": ["issue"],                 # 可检索列(hybrid:vss 向量 + fts 全文)
    },
]

db = await Seekbase.open("./data", schema=SCHEMA)

# 写是同步的:返回 ticket(已落库),wait 立即返回
await db.wait(await db.insert("cards", {"card_id": "c1", "issue": "pty vs tmux", "kind": "issue"}))

# 读是管道:纯 SQL 零管道、原样执行;检索是 search 源段 + 一条 SQL,一个接口全包
rows = await db.query(
    "SELECT card_id, issue FROM cards WHERE kind = ? ORDER BY created_at DESC LIMIT 20",
    params=["issue"],
)
hits = await db.query(
    "search cards 'pty 终端' | SELECT card_id, _score FROM _in ORDER BY _score DESC LIMIT 10",
)   # 时光机同参:query(..., ds_end="20260601") 连 search 候选一起回溯

await db.delete("cards", where="card_id = ?", params=["c1"])   # 打墓碑,永不物理删

await db.close()
```

## 服务形态(HTTP)

起一个 server——它持有 schema(以及将来的 embedder),并拥有数据目录。`seekbase_server(db)` 给你一个裸 ASGI app,用**你自己的 runner** 跑:

```python
# serve.py —— 用外部注入的 runner(这里是 uvicorn)
import uvicorn
from seekbase import Seekbase
from seekbase.server import seekbase_server

db = await Seekbase.open("./data", schema=SCHEMA)        # 就是上面那个嵌入 db
uvicorn.run(seekbase_server(db, api_key="secret"), host="0.0.0.0", port=8000)
```

也有个便捷函数 `serve(db, host=…, port=…, api_key=…, runner=…)`:`runner` 是任意 `runner(app, host=…, port=…)` 可调用(默认用 uvicorn,前提是你装了它)。runner 始终由外部提供,seekbase 不把它作为依赖。

然后从任何地方连它——**调用代码和嵌入形态一模一样**,只有拿句柄这一步不同:

```python
db = await Seekbase.connect("http://localhost:8000", api_key="secret")

await db.wait(await db.insert("cards", {"card_id": "c1", "issue": "pty vs tmux", "kind": "issue"}))
rows = await db.query("SELECT card_id, issue FROM cards WHERE kind = ?", params=["issue"])

await db.close()
```

读走 `POST /v1/query`、写走 `POST /v1/insert`(同步,返回已 done 的 ticket)。**错误过线保型**(server 侧抛的 `ReadOnlyError`,client 侧还是 `ReadOnlyError`)。鉴权是一个可选的 bearer token;时光机走 `query(..., ds_end="20260601")`,HTTP 上一样。

## 设计原则

- **只增、引擎强制**:没有 `update`/`upsert`;`delete()` 只写一列 `deleted_at` 墓碑。历史因此诚实——时光机对**所有列**都严谨。
- **业务无关**:不认识任何业务概念、不读任何 config——由你注入 `data_dir`、`schema`,以及(要 search 时)一个 `embedder`。
- **调用方永远不见向量**:声明 `searchable` 列;管道里 `search 表 '文本'` 自动 embed + jieba 分词 + hybrid 检索(每个可搜列各自一套 vss 向量 + fts 全文,RRF 融合),产 `_in` 表交给下一段 SQL 和结构化过滤组合。
- **接缝才切**:`|` 只标 DuckDB 自己跨不过去的接缝;一条 SQL 能干完的事绝不拆段——整条管道编译成一条 `WITH` SQL,优化器看穿全链。

## 文档

- [DESIGN.md](DESIGN.md) —— 整体设计
- [docs/api/](docs/api/) —— API 参考(query / insert / delete / admin / setup,每个接口的请求·响应·错误)
- [docs/works/](docs/works/) —— 专题设计。**架构主线**:[pipeline-as-anything.md](docs/works/pipeline-as-anything.md)(SPL 式管道 / SQL 一等公民 / 一切皆表)、[operator-plugin.md](docs/works/operator-plugin.md)(`Operator` 基类:两轴四方法)、[operator-registry.md](docs/works/operator-registry.md)(万物皆注册算子 + 能力×策略限权)、[pipeline-runtime-optimize.md](docs/works/pipeline-runtime-optimize.md)(降级到 runtime / 不自建执行器)。**子系统**:[store.md](docs/works/store.md)(两层存储 files / 派生=结构化 DuckDB+可插拔检索后端)、[search.md](docs/works/search.md)(检索作为 source 段,引擎可插拔 lance/duck-vss)

Apache-2.0。
