# 函数形态(Python)

函数形态是 [HTTP 形态](http.md) 的客户端:每个调用就是**在本地或远端构造一个 `POST /v1/execute` 请求**。同一套调用代码,`open`(进程内)与 `connect`(HTTP)通用——差别只在你怎么拿 `db`。

## 拿到 db 句柄

### `Seekbase.open` — 嵌入(进程内 DuckDB)

```python
db = await Seekbase.open(
    data_dir,           # str | Path:实例目录,自动创建
    *,
    schema=SCHEMA,      # 声明式表结构(见 schema.md)
    embedder=None,      # Embedder;schema 有 searchable 列时必填(见 embedders.md)
    as_of=None,         # None=当前态(可写);ISO 时间点=只读时光机
)
```

### `Seekbase.connect` — HTTP 客户端

```python
db = await Seekbase.connect(
    url,                # "http://localhost:8000"
    *,
    api_key=None,       # bearer token(server 配了才需要)
    as_of=None,         # 只读回退,随每请求带给 server
    transport=None,     # 可选 httpx transport(测试用 ASGITransport)
)
```

- **不做握手**:第一次真正查询才打到 server。
- schema 与 embedder 都在 **server 端**,客户端不带。

### 通用:`ready` / `close` / 上下文管理器

```python
db.ready            # bool(对应 GET /v1/health 的 ready)
await db.close()
async with await Seekbase.open(data_dir, schema=SCHEMA) as db:
    ...             # 退出自动 close()
```

## 查询链(ORM)

`db.table(name)` 返回**惰性、不可变**的 `QueryBuilder`,`await` 才执行。每个终结算子对应 [http.md 操作表](http.md#操作表op) 的一个 `op`。

```python
# select → op:"select"
rows = await (db.table("cards")
    .select("card_id", "issue")             # 省略=声明列 + created_at
    .eq("kind", "issue").gte("created_at", "2026-06-01")
    .order("created_at", desc=True).limit(20).offset(0))

# count → op:"count"
n = await db.table("cards").in_("card_id", ["c1", "c2"]).count()

# insert → op:"insert"(只增)
await db.table("cards").insert({"card_id": "c1", "issue": "pty tmux", "kind": "issue"})
await db.table("cards").insert([{...}, {...}])          # 批量

# delete → op:"delete"(打墓碑,返回受影响行数)
tombstoned = await db.table("cards").delete().eq("card_id", "c1")

# search → op:"search"  [M3]
hits = await db.table("cards").search("pty tmux").eq("kind", "issue").limit(10)
```

| 算子 | HTTP `op` | `await` 返回 |
|---|---|---|
| `select(*cols)` | `select` | `list[dict]` |
| `count()` | `count` | `int` |
| `insert(row \| rows)` | `insert` | `None` |
| `delete()` | `delete` | `int` |
| `search(text)` | `search` `[M3]` | `list[dict]`(带 `_score`) |
| 过滤 `eq neq gt gte lt lte in_ like ilike is_` | → `predicates` | (链式) |
| `order(col, desc=) limit(n) offset(n)` | → `orders`/`limit`/`offset` | (链式) |

- **只增、引擎强制**:没有 `update`/`upsert`;`delete()` 唯一语义是打 `deleted_at` 墓碑,正常查询自动滤掉。
- 未知列 → `QueryError`;`in_([])` 匹配空集;`is_(col, None)` → `IS NULL`。

## SQL 与管理动作

```python
rows = await db.sql("SELECT kind, count(*) FROM cards GROUP BY kind")  # op:"sql",只读
await db.flush()                    # op:"flush"(读己之写)          [M3] no-op
await db.rebuild()                  # op:"rebuild"(从文件重建)       [M2]
await db.vacuum(before="2026-06-01T00:00:00Z")  # op:"vacuum"(丢历史) [M4]
```

- `sql()` **只读**:语句须以 `SELECT`/`WITH` 开头,否则 `ReadOnlyError`——写只能走 ORM。
- 语义详解见 [http.md 操作表](http.md#操作表op)。

## Server 启动

把一个嵌入 `db` 暴露成 HTTP 服务。

```python
from seekbase.server import seekbase_server, serve

db = await Seekbase.open("./data", schema=SCHEMA, embedder=embedder)  # server 持有 schema/embedder

# 方式一:拿裸 ASGI app,用你自己的 runner 跑
app = seekbase_server(db, api_key="secret")
import uvicorn; uvicorn.run(app, host="0.0.0.0", port=8000)

# 方式二:便捷函数;runner 外部注入,缺省用 uvicorn(装了才行)
serve(db, host="0.0.0.0", port=8000, api_key="secret", runner=None)
```

- `seekbase_server(db, *, api_key=None)` → 裸 ASGI app(**零 web 框架依赖**)。
- `serve(db, *, host, port, api_key=None, runner=None)` → `runner` 是任意 `runner(app, host=…, port=…)` 可调用;**runner 始终外部提供**,不是 seekbase 依赖。
- 暴露的端点/线格式见 [http.md](http.md)。

## 错误(两形态相同)

```python
from seekbase import ReadOnlyError
try:
    await db.table("cards").insert(row)     # as_of 连接
except ReadOnlyError:
    ...
```

HTTP 形态下**错误保型过线**:server 侧的 `ReadOnlyError` 在客户端还原成 `ReadOnlyError`,同样的 `except` 生效。层级与映射见 [errors.md](errors.md)。
