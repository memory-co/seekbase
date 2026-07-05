# seekbase

一个 supabase 风格的数据端口,**把语义 `search()` 做成一等算子**——结构化/分析走 DuckDB,向量走 LanceDB,再加一份本地文件镜像用于审计。一个端口、一个目录、零运维。

**两种使用形态,一套完全相同的 API:**

| 形态 | 怎么拿到 db | 跑在哪 | 什么时候用 |
|---|---|---|---|
| **嵌入(embedded)** | `Seekbase.open(dir, schema=…)` | 进程内、DuckDB | 单进程、本地优先、零运维 |
| **服务(server)** | `Seekbase.connect(url)` → 连一个运行中的 server | HTTP | 多客户端 / 多进程共享同一个实例 |

两种形态的**调用代码逐字节相同**——`table().select()…` 链和 `search()` 一个字都不用改,变的只有你怎么拿到 `db` 句柄。

> **状态:早期骨架(M1)。** 结构化 ORM(`select` / `insert` / 墓碑式 `delete` / `count`)、只读 SQL 直查、部分 as-of 时光机,**以及两种使用形态**,今天都能跑。向量 `search()`、outbox、文件镜像、`rebuild()` 和 `vacuum()` 在后续里程碑落地。完整设计见 [DESIGN.md](DESIGN.md)。

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
        "searchable": ["issue"],                 # (向量引擎落地后生效)
    },
]

db = await Seekbase.open("./data", schema=SCHEMA)

# 写是异步的:返回 ticket,可 wait 到落库
await db.wait(await db.insert("cards", {"card_id": "c1", "issue": "pty vs tmux", "kind": "issue"}))

# 读是 SQL:结构化 + 时光机(ds_start/ds_end)+ 语义 search() 都在这一个接口
rows = await db.query(
    "SELECT card_id, issue FROM cards WHERE kind = ? ORDER BY created_at DESC LIMIT 20",
    params=["issue"],
)

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

读走 `POST /v1/query`、写走 `POST /v1/insert`(异步 ticket)。**错误过线保型**(server 侧抛的 `ReadOnlyError`,client 侧还是 `ReadOnlyError`)。鉴权是一个可选的 bearer token;时光机走 `query(..., ds_end="20260601")`,HTTP 上一样。

## 设计原则

- **只增、引擎强制**:没有 `update`/`upsert`;`delete()` 只写一列 `deleted_at` 墓碑。历史因此诚实——时光机对**所有列**都严谨。
- **业务无关**:不认识任何业务概念、不读任何 config——由你注入 `data_dir`、`schema`,以及(要 search 时)一个 `embedder`。
- **调用方永远不见向量**:声明 `searchable` 列;`search(text)` 自动 embed + 检索 + 在同一条链上和结构化过滤组合。

## 文档

- [DESIGN.md](DESIGN.md) —— 整体设计
- [docs/api/](docs/api/) —— API 参考(query / insert / delete / admin / setup,每个接口的请求·响应·错误)
- [docs/works/](docs/works/) —— 专题设计:[store.md](docs/works/store.md)(三写形态 files/DuckDB/LanceDB)

Apache-2.0。
