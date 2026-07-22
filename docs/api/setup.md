# Setup:拿句柄 / 起 server / schema / embedder / 策略与算子

数据接口见 [query](query.md) / [insert](insert.md) / [delete](delete.md) / [admin](admin.md)。本页讲怎么把库跑起来:拿 `db` 句柄、起 HTTP server、声明 schema、注入 embedder。

## 1. 拿句柄

### `Seekbase.open` — 嵌入(进程内 DuckDB)

```python
db = await Seekbase.open(
    data_dir,                # str | Path:实例目录,自动创建
    *,
    schema=SCHEMA,           # 声明式表结构(见 §3)
    embedder=None,           # Embedder;schema 有 searchable 列时必填(见 §4)
    search_backend="vss",    # "vss" | "lance":检索引擎后端
    policy=None,             # Policy;缺省 read-only(见 §5)
    operators=None,          # 自定义算子列表(见 §5)
)
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `data_dir` | 是 | 实例目录;`duck.db` / `files/`(canonical 镜像)/ `tasks/`(task 日志 + 结果)/ `lance/`(lance 后端)都落这里,拷走目录 = 拷走整个库 |
| `schema` | 是 | 声明式表结构(§3) |
| `embedder` | 视情况 | schema 有 `searchable` 列时必填,否则 `EmbedderInvalid` |
| `search_backend` | 否 | `"vss"`(默认,DuckDB vss+fts 就地长在业务表)/ `"lance"`(LanceDB 侧数据集,经 duck `lance` 扩展);取舍见 [`../works/search.md` §5](../works/search.md) |
| `policy` | 否 | 算子授权策略,缺省 `Policy()`(read-only,`sh`/`jq` 被拒);§5 |
| `operators` | 否 | 追加注册的自定义算子(类或实例);§5 |

> 时光机 / 时间窗**不在连接上**——是 `query` 的 `ds_start`/`ds_end` 参数(见 [query.md](query.md#时间窗-ds_start--ds_end日期分区));句柄本身不绑时间。

### `Seekbase.connect` — HTTP 客户端

```python
db = await Seekbase.connect(
    url,                # "http://localhost:8000"
    *,
    api_key=None,       # bearer token(server 配了才需要)
    transport=None,     # 可选 httpx transport(测试用 ASGITransport)
)
```

- **不做握手**:第一次真正查询才打到 server。
- schema 与 embedder 都在 **server 端**,客户端不带;之后 `db.query(...)` / `db.insert(...)` 用法与嵌入**完全相同**。

### 通用:`ready` / `close`

```python
db.ready            # bool(对应 GET /v1/health 的 ready)
await db.close()    # 嵌入:停流 → 取消后台 task → 停写 worker → 关 DuckDB;客户端:关 httpx
async with await Seekbase.open(data_dir, schema=SCHEMA) as db:
    ...             # 退出自动 close()
```

## 2. 起 server

server 持有 schema 与 embedder、拥有数据目录;客户端 `connect` 连它。

```python
from seekbase.server import seekbase_server, serve

db = await Seekbase.open("./data", schema=SCHEMA, embedder=embedder)

# 方式一:拿裸 ASGI app,用你自己的 runner 跑
app = seekbase_server(db, api_key="secret")
import uvicorn; uvicorn.run(app, host="0.0.0.0", port=8000)

# 方式二:便捷函数;runner 外部注入,缺省用 uvicorn(装了才行)
serve(db, host="0.0.0.0", port=8000, api_key="secret", runner=None)
```

| 函数 | 说明 |
|---|---|
| `seekbase_server(db, *, api_key=None)` | 返回裸 ASGI app(**零 web 框架依赖**),挂进任意 ASGI server |
| `serve(db, *, host, port, api_key=None, runner=None)` | 便捷启动;`runner` 是任意 `runner(app, host=, port=)` 可调用,**始终外部提供**,不是 seekbase 依赖 |

暴露的端点见 [README.md](README.md)。

## 3. 声明 schema

表结构声明一次,DDL / vss+fts 检索派生 / 文件镜像全由 seekbase 管。`open` / server 启动时校验一次——**坏形状当场报错**。设计与推导(一处声明 → 单引擎 DuckDB + 文件)见 [`../works/schema.md`](../works/schema.md)。

**SCHEMA 是有序列表**(表名做 `table` 字段),列也是有序列表 `{name, type}`;主键单独走 `primary` 字段:

```python
SCHEMA = [
    {
        "table": "cards",
        "columns": [
            {"name": "card_id", "type": "str"},
            {"name": "issue",   "type": "str"},
            {"name": "kind",    "type": "str"},
        ],
        "primary": "card_id",
        "searchable": ["issue"],                 # 可选:可被 search 段检索的列(写入自动 embed + jieba 分词)
    },
]
```

**`columns`** —— 有序列表,每项 `{name, type}`:

- 类型:`str` / `int` / `float` / `bool` / `decimal(p,s)` / `timestamptz` / `json`。
- **声明式、不从首行推断**(避免首行 null 把列判成 string)。
- `ds` / `created_at` / `deleted_ds` / `deleted_at` 是**引擎代管的元数据列**,自动加、**不许自己声明**:`ds`/`deleted_ds`(天,`YYYYMMDD`,分区 / 时光机判定)+ `created_at`/`deleted_at`(精确时刻)。见 [`../works/time_machine.md`](../works/time_machine.md)。

**`primary`** —— 表级单独字段,主键列名。每表有且仅有一个;须是 `str` / `int` 列;**写一次**(重复主键报错);是各层(DuckDB 物理表 / 文件)对齐的锚。

**`searchable`**(可选)—— 哪些列可被管道的 `search` 段检索(hybrid:向量 + BM25 全文,RRF 融合),**必须是 `str` 列**。声明了 ⇒ **必须注入 embedder**(否则 `EmbedderInvalid`);没有则是纯结构化表、零检索开销。检索扩展(`vss`/`fts` 或 `lance`,按 `search_backend`)在 `open()` 时自动 `INSTALL/LOAD`(首次需联网),中文分词用内置 `jieba`。

**文件镜像**:**每表自动**落成按天分区的 `<表>.jsonl`(无 `files` 声明),详见 [`../works/store.md`](../works/store.md)。

**校验规则**(`seekbase.schema.parse_schema`)

| 规则 | 违反 → |
|---|---|
| `SCHEMA` 是列表;每项有 `table`(唯一)| `SchemaError` |
| `columns` 是列表;每项 `{name, type}`,列名唯一 | `SchemaError` |
| `primary` 指向一个已声明的 `str`/`int` 列 | `SchemaError` |
| 不许声明 `ds`/`created_at`/`deleted_ds`/`deleted_at` | `SchemaError` |
| 列类型合法(含 `decimal(p,s)` 的 `p`/`s`)| `SchemaError` |
| `searchable` 列须是已声明的 `str` 列 | `SchemaError` |
| 有 `searchable` 却无 embedder | `EmbedderInvalid` |

## 4. 注入 embedder

seekbase 核心只认一个**注入协议**,不绑定模型。调用方永远不见向量——只写文本。

```python
class Embedder(Protocol):
    @property
    def dim(self) -> int: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...   # 可同步可异步
```

默认实现 `ApiEmbedder`(核心自带,基于 `httpx`,不加载本地模型):

```python
from seekbase.embedders import ApiEmbedder

embedder = ApiEmbedder(
    base_url="https://api.openai.com/v1",   # OpenAI 兼容 /embeddings
    api_key="sk-…", model="text-embedding-3-small", dim=1536,
    batch_size=128, max_retries=3, timeout=30.0,
)
db = await Seekbase.open(data_dir, schema=SCHEMA, embedder=embedder)
```

- 调 `POST {base_url}/embeddings`,取 `data[].embedding`;维度不符 / 失败到顶 → `EmbedderInvalid`。
- embedder 在 **server / 进程端**注入;`connect` 的客户端不带,embedding 在 server 上算。
- **TODO**:本地 sentence-transformers 版(`SentenceTransformerEmbedder`),同一协议(DESIGN §10)。

## 5. 策略与自定义算子(server 端配置)

管道里每个算子段按声明的**能力**(`PURE`/`FS_READ`/`NET`/`EXEC`…)受 `Policy` 约束,**编译期判定**、拒了管道不启动(HTTP 403 `PermissionDenied`):

```python
from seekbase import Policy, Cap, Operator

db = await Seekbase.open("./data", schema=SCHEMA, embedder=emb,
                         policy=Policy(mode="sandboxed"),      # 放行 sh/jq(进沙箱)
                         operators=[MyOperator])               # 自定义算子(和内建平权)
```

- 三模式:`read-only`(默认,拒 `EXEC`/`FS_WRITE`)/ `sandboxed`(放行 EXEC,子进程限 scratch cwd + 最小 env + 墙钟超时)/ `trusted`;判定顺序 **deny > allow > 模式缺省**。细节与示例见 [../sdk/policy.md](../sdk/policy.md)。
- 自定义算子 = 继承 `Operator` 的子类(写 `optimize_duck`/`optimize_bash` 原生降级),经 `operators=` 注册后在管道里与 `search`/`grep` 平权;作者指南见 [../sdk/operator.md](../sdk/operator.md)。
- 两者都是 **server 端**配置;HTTP 客户端无感(只会在越权时收到 403)。
