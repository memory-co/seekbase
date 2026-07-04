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
pip install seekbase            # 嵌入 + HTTP 客户端 + ApiEmbedder,开箱即用
pip install 'seekbase[server]'  # + uvicorn,用于把端口跑成一个 HTTP 服务
```

HTTP **客户端**(`Seekbase.connect`)只需要核心依赖(httpx 已在核心里)。`[server]` extra 只给**对外提供服务**的那个进程用。

## 嵌入形态(进程内)

```python
from seekbase import Seekbase

SCHEMA = {
    "cards": {
        "columns": {"card_id": "str primary", "issue": "str",
                    "kind": "str", "created_at": "str"},
        "searchable": ["issue"],                 # (向量引擎落地后生效)
    },
}

db = await Seekbase.open("./data", schema=SCHEMA)

await db.table("cards").insert({"card_id": "c1", "issue": "pty vs tmux", "kind": "issue"})

rows = await (db.table("cards")
    .select("card_id", "issue")
    .eq("kind", "issue")
    .order("created_at", desc=True)
    .limit(20))

await db.table("cards").delete().eq("card_id", "c1")   # 打墓碑,永不物理删

await db.close()
```

## 服务形态(HTTP)

先起一个 server——它持有 schema(以及将来的 embedder),并拥有数据目录:

```python
# serve.py
from seekbase import Seekbase
from seekbase.server import serve

db = await Seekbase.open("./data", schema=SCHEMA)        # 就是上面那个嵌入 db
serve(db, host="0.0.0.0", port=8000, api_key="secret")  # 阻塞运行;需要 seekbase[server]
```

如果你想用自己的 ASGI server(uvicorn/hypercorn/…)跑、或把它挂进一个更大的应用里,`create_app(db)` 会返回一个裸 ASGI app。

然后从任何地方连它——**调用代码和嵌入形态一模一样**,只有拿句柄这一步不同:

```python
db = await Seekbase.connect("http://localhost:8000", api_key="secret")

await db.table("cards").insert({"card_id": "c1", "issue": "pty vs tmux", "kind": "issue"})
rows = await db.table("cards").select("card_id", "issue").eq("kind", "issue").limit(20)

await db.close()
```

查询链会被序列化成一个 `POST /v1/execute`,server 执行后返回行。**错误过线保型**(server 侧抛的 `ReadOnlyError`,client 侧还是 `ReadOnlyError`)。鉴权是一个可选的 bearer token;时光机只读也能走 HTTP(`Seekbase.connect(url, as_of="2026-06-01T00:00:00Z")`)。

## 设计原则

- **只增、引擎强制**:没有 `update`/`upsert`;`delete()` 只写一列 `deleted_at` 墓碑。历史因此诚实——时光机对**所有列**都严谨。
- **业务无关**:不认识任何业务概念、不读任何 config——由你注入 `data_dir`、`schema`,以及(要 search 时)一个 `embedder`。
- **调用方永远不见向量**:声明 `searchable` 列;`search(text)` 自动 embed + 检索 + 在同一条链上和结构化过滤组合。

Apache-2.0。
