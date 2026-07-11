# seekbase — pip 包设计

> 把 [seekbase v5 设计稿](../memory.talk/docs/works/v5/seekbase.md)（概念层）落成一个**独立、可 `pip install` 的库**的工程设计。
>
> 概念不变:一个数据端口 = 类 supabase 的 ORM + `search()` 一等算子,**单引擎 DuckDB**(结构化 + 向量 `vss`/HNSW + 全文 `fts`/BM25)+ 文件镜像,焊死 insert-only(主键写一次),自带 ds 时光机。本文只讲**怎么把它做成包**:边界、依赖、目录、公共契约、内部架构、并发模型、测试、分期。
>
> **v1 范围决策(已定):完整愿景全做** · **embedder 纯注入 + 可选 `[st]` extra** · **async 优先**。

---

## 0. TL;DR

- 包名 `seekbase`,`import seekbase` / `from seekbase import Seekbase`。Python **3.11+**,Apache-2.0(仓库已有 LICENSE)。
- 运行时依赖:`duckdb`、`jieba`、`httpx`。**两种形态都是标配、不开任何 extra**——嵌入、HTTP 客户端(`connect`,靠 httpx)、把端口暴露成服务的 `seekbase_server(db)`(**零依赖手写 ASGI app**)。跑 app 的 **ASGI runner(uvicorn/hypercorn/…)由宿主外部注入**,不是 seekbase 依赖(和 Starlette/FastAPI 同路数:库给 app,runner 自带)。**向量 `vss` + 全文 `fts` 是 DuckDB 扩展**,`open()` 时 `INSTALL/LOAD`,不是额外 pip 依赖。embedder 走**纯注入**,默认实现 `ApiEmbedder`(OpenAI 兼容 `/embeddings`)也是核心一部分,核心仍**不加载任何本地模型**。本地 sentence-transformers 模式**记 TODO 后续做**(§10)。
- **业务无关**(继承 searchbase 纪律):包里不出现 card/round/session,不读任何 Config,只收明确的值(`data_dir` / `schema` / `embedder`)。
- 公共面就一个类 `Seekbase`(`query` 读 + 异步 `insert`/`delete` 写)+ 几个值类型/协议。引擎(DuckDB 结构化 + `vss`/`fts` 检索 + 文件/planner/时光机)全在 `_engine/` 后面,不导出。
- 一个 seekbase **实例 = 一个目录**:`<data_dir>/{duck.db, files/}`——**单个 DuckDB 文件**(结构化行 + 向量 + 倒排全在 `duck.db` 里),外加文件镜像。拷走目录 = 拷走整个库。

---

## 1. 包身份与边界

| 维度 | 取值 | 说明 |
|---|---|---|
| PyPI 名 | `seekbase` | import 名同名 |
| Python | `>=3.11` | 现代 typing、`asyncio.TaskGroup`、`tomllib` |
| License | Apache-2.0 | 仓库已带 |
| 形态 | 进程内嵌入库(async) | 云端版是**形态预留**,不实现(见 §9) |
| 依赖它的人 | 宿主应用的组装根 | 由宿主注入 embedder / 路径;seekbase 不认识业务、不读 Config |

**包的对外承诺(端口契约)**:调用方只见 SQL `query`(含 `search()`)+ 异步写(`insert`/`delete` + ticket)+ 少数管理动作(`rebuild/close`)。**永远看不见**:向量、embedding 计算、jieba 分词、DuckDB 连接句柄、`_vec`/`_tok` 列与 `vss`/`fts` 索引结构、文件落盘顺序。这条「不漏进程内假设」的纪律也是云端版能直接换 HTTP 的前提(§9)。

---

## 2. 依赖与打包

```toml
# pyproject.toml (hatchling, flat layout)
[project]
name = "seekbase"
requires-python = ">=3.11"
dependencies = [
    "duckdb>=1.1",       # 单引擎:结构化 + 向量(vss/HNSW)+ 全文(fts/BM25)
    "jieba>=0.42",       # 中文分词(fts/BM25 的 tok 列;索引侧与查询侧同一套切词)
    "httpx>=0.27",       # HTTP 客户端(connect)+ 默认 embedder(ApiEmbedder)
    # vss + fts 是 DuckDB 扩展,open() 时 INSTALL/LOAD,不占 pip 依赖
    # server 标配靠 seekbase_server()(零依赖 ASGI app);ASGI runner 由宿主外部注入,不绑定
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "ruff", "mypy"]
# TODO: st = ["sentence-transformers>=3"]  # 本地模型 embedder,后续做(§10)

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- **`vss` / `fts` 是 DuckDB 扩展**:`open()` 时 `INSTALL vss; LOAD vss; INSTALL fts; LOAD fts;`——**首次 open 需联网**拉扩展(或用预置/离线打包的扩展二进制),之后本地缓存。这是单引擎的打包代价,换来的是彻底去掉 LanceDB 这个进程外向量库。
- **pyarrow** 由 duckdb 传递带入,不直接声明(向量以 `list[float]` / Arrow 进出,零拷贝互通)。
- 默认 embedder 只需一个轻量 HTTP 客户端(`httpx`,已进核心依赖),不碰任何模型权重;`pip install seekbase` 装完不下载任何权重。**本地 `st` 模式暂不做,记 TODO(§10)。**
- **`jieba` 是纯 Python 中文分词**:DuckDB 的 `fts` 按空白切词,切不动中文,故 BM25 前先用 jieba(search 模式)把文本切成空格分隔的 token,索引侧与查询侧同一套。

---

## 3. 目录结构(flat layout)

```
seekbase/                      # 仓库根
  pyproject.toml
  LICENSE                      # Apache-2.0(已有)
  README.md                    # 一分钟上手(照 searchbase README 的手感)
  DESIGN.md                    # 本文
  seekbase/                    # 包(flat,不用 src/)
    __init__.py                # 对外导出:Seekbase + 值类型 + 协议 + 错误
    _types.py                  # Embedder 协议 + 错误层级 —— 行为契约(数据对象都在 struct/)
    client.py                  # Seekbase(async 门面:open/connect/query/insert/delete/wait/…)
    schema.py                  # SCHEMA 解析/校验 + 类型映射(结构体在 struct/schema.py,这里只 parse)
    server.py                  # 手写 ASGI 壳:auth + 收发 + 错误映射 → 派给 api/(seekbase_server / serve)
    api/                       # HTTP API 面:一类接口一个文件(目录即接口清单,对齐 docs/api/*.md)
      _route.py                #   Endpoint 数据类 + 无框架路径匹配(含 {param})
      query.py                 #   POST /v1/query            读(SQL + ds 时间窗)
      insert.py                #   POST /v1/insert           写(同步,写一次 PK)
      delete.py                #   POST /v1/delete           软删
      writes.py                #   GET  /v1/writes/{ticket}  查写状态
      rebuild.py               #   POST /v1/rebuild          admin:replay files→DuckDB
      health.py                #   GET  /v1/health           就绪探针
    _wire.py                   # Request 序列化 + 错误↔HTTP 状态码映射(client/server 共用)  [M1 已落]
    service/                   # 用例编排层(业务无关 CRUD+检索):把引擎串起来,一类用例一个文件
      query.py                 #   QueryService —— 读:search 抽取→hybrid→引擎 run_query
      write.py                 #   WriteService —— insert/delete:校验→embed→files-first→DuckDB
      admin.py                 #   AdminService —— rebuild:replay 文件镜像→重灌 DuckDB
      __init__.py              #   Services 装配(build_services 从引擎注入)
    struct/                    # 所有层间数据对象定义(frozen dataclass / 别名,无行为)
      request.py               #   Request —— 传输中立的 op 单元(port → executor)
      ticket.py                #   Ticket —— 写回执(service → port / wire;to_wire/from_wire)
      schema.py                #   Column / TableSpec / Schema + ds/… 元列常量(parse 在 ../schema.py)
      row.py                   #   Row / Hit —— 查询结果 dict 形态
    _engine/                   # 机制层:只提供原语,不做编排
      executor.py              # LocalExecutor(dispatch Request→service)/ HttpExecutor(→HTTP,回 Ticket)
      duck.py                  # DuckdbEngine:纯 DuckDB 原语——建表/可见性视图/existing_keys/commit_rows/soft_delete/run_query
      search.py                # SearchEngine:业务表上的 vss(HNSW)+ fts(BM25),embed/tok/hybrid/rebuild_fts
      rewrite.py               # search() 的 SQL 改写:extract_searches / search_target(纯函数)
      text.py                  # jieba 中文分词(fts 的 _tok 列;索引侧/查询侧同一套)
      clock.py                 # 引擎时钟:ds(YYYYMMDD)/ created_at(ISO)统一格式
      bridge.py                # async↔sync 桥(单线程 executor,串行化 DuckDB)  [M1 已落]
      files.py                 # FileMirror:按天分区、每表 <表>.jsonl append + iter_events  [M2 已落]
      # 写同步:WriteService 校验+embed → files 先落 → duck.commit_rows(随行 INSERT 含 _vec/_tok + FTS 重建)
      # 时光机 = duck 可见性视图的单表 ds 谓词(ds/deleted_ds);ds_start/ds_end 是 query 参数
    embedders/
      __init__.py              # Embedder 协议再导出
      api.py                   # 默认 ApiEmbedder(OpenAI 兼容 /embeddings,async httpx,核心自带)  [M1 已落]
      # TODO: sentence_transformer.py  # 本地模型 embedder,后续做(§10)
  tests/                       # 按场景组织(照 memory.talk):每个子目录 = 一个场景
    conftest.py                # 共享 fixture/helper:db / pair / open_db / FakeEmbedder
    README.md                  # 场景一览表 + 加新场景的规矩
    quickstart/                # 最基础端到端:开库→写→查→删→再查
    read_write/                # SQL query 读 + 异步 insert/delete round-trip
    file_mirror/               # 文件镜像:写落 jsonl、删是 append 墓碑、rebuild 重灌
    search/                    # SQL 里的 search():排序、结构化/时间窗组合、删后搜不到
    embedder_live/             # 真实 embedding API 端到端(需 env,默认 skip)
    insert_only/               # delete 只打墓碑、无 update 路径
    time_machine/              # ds_start/ds_end 时间窗 + 只读闸(嵌入)
    schema/                    # SCHEMA 校验(list 形态)+ 未知列拒 + searchable 需 embedder
    server/                    # server 形态:同一套调用走 HTTP(in-process ASGITransport)
    readonly_guard/            # query 只读:写/DDL/WITH…DML/多语句一律 ReadOnlyError
```

> **测试组织照 memory.talk 的「按场景」路数**:每个场景一个目录,内含 `README.md`(测什么 / 不测什么 / fixture 来源)+ `test.py`;`python_files` 收 `test.py`。相关用例合并到一个场景下,跟「按代码模块切文件」解耦。

**与 searchbase 的映射**:searchbase 的 LanceDB `local` 那摊(embed、ANN、维护协程,尤其**压缩 / EMFILE 恢复 / 连接重连**——都是为对付 LanceDB 版本化碎片文件的 fd 耗尽)**整体消失**:向量搬进 DuckDB 的 `vss`,全文用 `fts`,统一由 `_engine/search.py` 管。单文件存储让 fd 数恒定,那套 EMFILE 恢复机械不再需要(见 §6.1)。searchbase 的端口 `SearchBackend` **不再对外**——上层只 import `seekbase.Seekbase`。

---

## 4. 公共契约(端口)

### 4.1 打开 / 连接 / 关闭

```python
from seekbase import Seekbase

db = await Seekbase.open(data_dir, schema=SCHEMA, embedder=embedder)   # 嵌入(进程内 DuckDB)
db = await Seekbase.connect(url, api_key=…)                            # server(HTTP,同一套调用)
await db.close()
```

- **async 类工厂**:开库 + 起后台协程都要 await。`db.ready` (property):底层可用性,`False` → 宿主回 503。
- 上下文管理器糖:`async with await Seekbase.open(...) as db:`。时间窗**不绑连接**(是 `query` 参数,§4.2)。

### 4.2 读:一个 SQL `query` 接口

```python
rows = await db.query(
    "SELECT card_id, issue FROM cards WHERE kind = ? ORDER BY created_at DESC LIMIT 20",
    params=["issue"],          # 位置参数,绑定 ?(防注入)
    ds_end=None,               # 时光机 / 时间窗:ds_start / ds_end(YYYYMMDD,§7)
)   # → list[dict]
```

- **只读(强制)**:按 DuckDB 的**语句类型**判定必须是单条 `SELECT`——挡住 `WITH…DELETE` / 多语句这类「首词是 SELECT/WITH」的绕过。结构化查询、语义 `search()`(§4.3)、时光机(`ds_start`/`ds_end`)全在这一个接口。
- **自动滤墓碑**:引擎给每张表挂一个**可见性视图**——`deleted_ds IS NULL`(当前态)或 as-of 谓词(见 §6.5);`ds_start`/`ds_end` 圈定 day 区间。

### 4.3 写:`insert` / `delete`(同步,ticket)+ `search()`

```python
t = await db.insert("cards", {"card_id": "c1", "issue": "…", "kind": "issue"})  # 返 ticket(已 done)
await db.wait(t)                                     # 同步落库,立即返回
await db.delete("cards", where="card_id = ?", params=["c1"])   # 软删墓碑,永不物理删
```

- **写是同步的**:insert 内联 embed + jieba 分词,files + DuckDB 行(含向量)在一次调用里落地、FTS 同步重建;`ticket` 返回即 `done`(无异步兑现),`wait` 立即返回。API 保留 ticket 只为两形态对称。
- **焊死不变性**:端口无 `update`/`upsert`;**主键写一次**——重复主键 insert 直接 `QueryError`(不覆盖、不追加版本)。用户侧只读由 `query` 的语句守卫强制;引擎内部的软删标记 / 向量写入是受控写。(检索列是可重建投影,canonical 在文件,§6.1。)
- **`search(列, '文本')` 是 SQL 里的函数**(§4.2 的 `query` 内):`SELECT *, _score FROM cards WHERE search(issue, 'pty tmux') AND kind='issue' ORDER BY _score`,**hybrid**——向量语义(`vss`/cosine)+ BM25 关键词(`fts`)用 **RRF 融合**成一个分,自动 embed + 中文 jieba 分词,调用方永不见向量(每个 `searchable` 列各自一套 vss+fts,§6.3)。一条 query 可有多个 `search()`(搜不同列),各自暴露一个 `_score_<列>`;单个 `search()` 时额外附便捷别名 `_score`。

### 4.4 管理动作

```python
await db.rebuild()               # 按 ds 顺序 replay 全部 <表>.jsonl → 清空并重灌 DuckDB 物理表(重新 embed + 重建 vss/fts)
```

- `rebuild` 返回 `ticket`(同步,已 done)。「派生层可重建、canonical 在文件」的兑现。**没有物理删 / vacuum**:delete 永远只是软删墓碑,历史永久。

### 4.5 声明式 SCHEMA

```python
SCHEMA = [                                            # 有序列表,建表顺序 = 列表顺序
    {
        "table": "cards",
        "columns": [                                 # 有序列表,列序 = DDL 列序
            {"name": "card_id", "type": "str"},
            {"name": "issue",   "type": "str"},
            {"name": "kind",    "type": "str"},
        ],
        "primary": "card_id",                        # 主键列名(单独字段)
        "searchable": ["issue"],                     # 这列可 search()
    },
]
```

- `columns` 有序列表,每项 `{name, type}`;类型 `str/int/float/bool/decimal(p,s)/timestamptz/json`。主键单独走 `primary` 字段(须 `str`/`int`)。**声明式、不从首行推断**(避免 null→错判列型,searchbase 已踩过)。完整设计 [works/schema.md](docs/works/schema.md)。
- `ds` / `created_at` / `deleted_ds` / `deleted_at` 是**引擎代管的元数据列**:schema 没写也自动加、**不许自己声明**(时光机靠这两对日期字段,§6.5 / [works/schema.md](docs/works/schema.md) / [works/time_machine.md](docs/works/time_machine.md))。
- `searchable` = 「search 自动模糊查询」的开关:声明了 → 业务表多出 `_vec_<列>`/`_tok_<列>` 两列 + vss/fts 索引,insert 自动 embed + jieba 分词随行写入、search 自动 hybrid 查;没声明的表就是**纯结构化表,零检索开销**。
- **文件镜像每表自动**(无 `files` 声明):每张表落成按天分区的 `<表>.jsonl`(见 §6.6 / [works/store.md](docs/works/store.md))。
- **schema 校验在 open 时做一次**:主键唯一、searchable 列存在、embedder 存在性(有 searchable 列却没给 embedder → 明确报错)。

### 4.6 值类型与协议(`_types.py`)

```python
class Embedder(Protocol):            # 注入契约(sync 或 async 都收)
    @property
    def dim(self) -> int: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...   # 可返回 awaitable

Row  = dict                          # 普通 dict 行(v1)
Hit  = dict                          # 同 Row,每个 search() 多一个 "_score_<列>": float(单 search 另附 "_score")

# 错误层级
class SeekbaseError(Exception): ...
class SeekbaseUnavailable(SeekbaseError): ...   # 底层开不了 → 宿主回 503
class SchemaError(SeekbaseError): ...           # open 时 schema 校验失败
class EmbedderInvalid(SeekbaseError): ...       # embedder 维度/契约不符
class ReadOnlyError(SeekbaseError): ...         # query 传了非单条 SELECT(按语句类型判定)
class NotFound(SeekbaseError): ...              # ticket 不存在 → 404
```

- Embedder 协议**同时容忍 sync/async**:内部 `await maybe_await(embedder.embed(...))`(检测 coroutine)。
- **v1 默认 `ApiEmbedder`(核心自带,开箱即用)**:async httpx 调 OpenAI 兼容 `/embeddings`,构造收 `base_url / api_key / model / dim`,内部批量 + 退避重试。端口仍只认 `Embedder` 协议(可换任意注入实现),但默认这一个进核心、不另开 extra。
- **本地 sentence-transformers embedder = TODO**(§10):后续加 `seekbase[st]` + `SentenceTransformerEmbedder`,同一协议,零改端口。

---

## 5. 实例布局(磁盘)

```
<data_dir>/                       # 一个 Seekbase 实例 = 一个目录(拷走=完整备份)
  duck.db                         # DuckDB 单文件:每业务表一张物理表(业务列 + ds/created_at/deleted_ds/deleted_at + 每可搜列 _vec/_tok,vss+fts 就地索引)
  duck.db.wal                     # DuckDB WAL
  files/                          # 文件镜像(canonical):顶层按 ds=YYYYMMDD 日期分区,内每表一个 <表>.jsonl
```

- **单文件、恒定 fd**:结构化 + 向量 + 全文全在 `duck.db`,打开的文件描述符数恒定(就 `duck.db` + WAL 两个),彻底避开 LanceDB 版本化碎片文件的 EMFILE/fd 耗尽(§6.1)。
- 只有一个 DuckDB 引擎 + 一份文件镜像共处一个目录,对齐 searchbase 的 `name="v1"` 实例化路数。(`_meta.json` schema/dim 指纹是 §10 待定项。)

---

## 6. 内部架构

### 6.1 单引擎一端口(§3 概念的落地)

```
              Seekbase(client.py:门面 query/insert/delete)
                            │  executor.py 编排
        ┌───────────────────┴───────────────────┐
   FileMirror                          DuckdbEngine(duck.db 单文件)
   (files.py) canonical         每业务表一张物理表 _sb_<表>
   每表 jsonl append      业务列 + ds/created_at/deleted_ds/deleted_at
                         + 每可搜列 _vec_<列>(vss/HNSW)/ _tok_<列>(fts/BM25)
        └───── 行/过滤/聚合/join + 可见性视图 + hybrid RRF,全在一张表 ─────┘
```

- **两层存储**:文件镜像是 **canonical**(§6.6);DuckDB 是**派生、可从文件重建**——每张业务表一张物理表 `_sb_<表>`,一行一主键(**写一次**),行里同时带业务列 + 元数据 + 每个 `searchable` 列的 `_vec_<列>`(`vss`/HNSW 索引)/ `_tok_<列>`(`fts`/BM25 索引)。检索列就是原来 LanceDB 扮演的角色,现在收进业务表本身、同一个 `duck.db`,没有单独的向量库/派生表/outbox。
- **为什么合并成单引擎(核心动因)**:LanceDB 是**版本化、每写生成碎片文件**的存储,配上每次操作开 table 句柄,在 memory.talk 里反复撞 `Too many open files (os error 24)`,要靠不停 compaction + 关连接重连来放 fd,背了一整套 EMFILE 恢复机械。**DuckDB 单文件让打开的 fd 数恒定**(就 `duck.db` + WAL),这类 fd 耗尽从结构上消失,那套恢复机械整个删掉。**代价(诚实讲)**:约束从 fd 转到内存(HNSW 常驻 RAM)+ FTS 周期重建 + HNSW 落盘仍标 experimental(§6.3 / [works/search.md](docs/works/search.md))。

### 6.2 写入流水与一致性(file ≥ DuckDB,同步)

`insert()` 顺序焊死,**同步**:

```
① 校验主键不得已存在(已存在 → QueryError,什么都不写);inline embed + jieba 分词
② FileMirror 写 JSONL(append + fsync 落盘)                     ← canonical 先落地
③ 一个 DuckDB 事务:INSERT 一行(业务 + ds/created_at + _vec/_tok)→ 同步重建该表 FTS
   → ticket 立即 done(向量随行落库,没有异步兑现)
```

- **为什么同步内联、不走 outbox**:主键**写一次**,所以一行的向量在 `INSERT` 时就定、之后永不 UPDATE(只软删)。这既避开了 DuckDB 实验性 on-disk HNSW 的 `UPDATE NULL→向量` 段错误(§6.3 / [works/search.md](docs/works/search.md)),也让写入同步——不需要 outbox/consumer 那套异步兑现。
- **HNSW 一次写定、FTS 同步重建**:`_vec_<列>` 随行 `INSERT`、写定不改;FTS 是**静态快照**,每次 insert 同步 `create_fts_index(..., overwrite=1)` 重建那张表——`insert` 返回即 `search()` 能搜到。
- **一致性关系固定可推理**:`file ≥ DuckDB`。file 面永不缺数据(至多瞬时超前一步,crash 后 repair 收敛);DuckDB 面强一致且**检索同步**(向量随行落库,无异步窗口)。`wait(ticket)` 立即返回。
- `delete()` 同路(同步):给那一行 `UPDATE deleted_ds/deleted_at`(软删,行不删、只动非索引列);文件侧往**删除日** `ds=X/<表>.jsonl` append 一条墓碑记录(纯 append,不回改已写行)。

### 6.3 search():hybrid(vss+fts)+ RRF,再 join 回主查询

`search(列, '文本')` 在 SQL 里的落地(完整设计见 [works/search.md](docs/works/search.md)):

```
无 search()  → 纯结构化:谓词→WHERE、order/limit/offset→SQL,一次查询(不碰 vss/fts)
有 search()  → ① executor 把查询文本 embed 成向量 + jieba 分词成 token
               ② 在业务表 _sb_<表> 的检索列上:
                    vss:ORDER BY array_cosine_distance(_vec_<列>, ?) LIMIT k    → 走 HNSW 索引
                    fts:ORDER BY match_bm25(pk, ?, fields:='_tok_<列>') DESC LIMIT k → 走 BM25(限定该列)
                  两路(均带 deleted_ds IS NULL)各出 top-k,用 RRF(1/(60+rk) 相加)融合成 (pk, _score_<列>)
               ③ 把 (pk, score) 灌进临时表,LEFT JOIN 进主表的可见性视图(§6.5)
                  → 结构化谓词 / ds 时间窗 / 排序全在同一条外层 SQL 里
```

- **检索候选来自当前存活行,时光机由可见性视图兜**:vss/fts 返回 `deleted_ds IS NULL` 的候选,外层 join 可见性视图(as-of `ds_start`/`ds_end`)再叠加 ds 时间窗裁剪。
- **每列独立**:每个 `searchable` 列一套 vss+fts,`search(列, …)` 只搜那一列;一条 query 多个 `search()` 各自 `_score_<列>`(§4.3)。
- **RRF 而非分数直加**:vss 的 cosine 距离和 BM25 分不同量纲,按**名次**融合(reciprocal rank fusion,k0=60)最稳,不用调权重。

### 6.4 async↔sync 桥与并发(`_bridge.py`)

- DuckDB 本身同步。**单写者模型**:一个专属**单线程 executor** 持有唯一 DuckDB 连接,所有 DuckDB 操作(结构化 + vss + fts)在该线程串行化(避免跨线程连接 + 满足 DuckDB 单写者)。单引擎后连接只有一条,fd 恒定(§6.1)。
- 写是**同步**的:insert 只有 embedding(可能 async、走网络)在 executor 外先算好,向量/tok 随行 `INSERT`、FTS 重建都排进同一单线程 executor(与前台读串行,天然不打架)。没有后台 consumer / outbox。
- **file 面无锁、不经 executor**:`grep`/`cat`/`diff` 直接读文件树,不占 DuckDB 连接、不被写入阻塞(靠 insert-only + 原子落盘兜底)。
- v1 单连接串行化优先做对;**读写分连接**(前台读走独立只读连接,避开 FTS 重建期的排队)+ 并发读优化列 §10。

### 6.5 时光机 = 日期分区(`ds`)

时光机用**离线大数据那套分区**实现,不靠谓词改写。**完整设计(两对日期字段、可见性谓词)见 [works/time_machine.md](docs/works/time_machine.md)**;这里给要点。

- **两对引擎代管日期字段**:创建 `(ds, created_at)` + 删除 `(deleted_ds, deleted_at)`;`_ds` 是天(`YYYYMMDD`,分区/时光机判定),`_at` 是精确时刻。**只有创建日不够**——判断「as-of D 时该行是否已删」必须有删除日 `deleted_ds`(否则早创建、晚删除的行会被错判)。
- **可见性 = 一句 ds 谓词**:每主键在 `_sb_<表>` 里恰好一行(**写一次**)。as-of D = `ds <= D AND (deleted_ds IS NULL OR deleted_ds > D)`;当前态 = `deleted_ds IS NULL`。因为主键写一次、没有值版本,时光机穿越的是**创建 / 删除两个时点**,不穿越同一 key 的改值(改值即报错),故不需要多版本重放。见 [works/time_machine.md](docs/works/time_machine.md)。
- **机制 = 分区裁剪**:`query` 带 `ds_start` / `ds_end`(闭区间)。只给 `ds_end` = 时光机(as-of ds_end);只给 `ds_start` = 那天之后仍活;两个都给 = 该窗口创建、且 ds_end 时仍活。时间窗是**查询参数、不绑连接**。
- **文件即分区**:文件镜像用 `ds=YYYYMMDD` 做**顶层目录**;insert 落创建日分区、delete 墓碑落删除日分区,`ls files/ds=20260705/` = 当天发生的事(建的行 + 删的墓碑)。
- **严谨性靠受控写 + 分区**:文件纯 append、永不回改;派生 DuckDB 行的 `deleted_ds` 由 delete 软删置上(派生可改、canonical 不改)。
- **没有物理删 / vacuum**:delete 永远只是软删墓碑,被删的行永久留着,时光机能倒回任意时刻、永不丢历史。文件真·纯 append、一次都不回改(零例外)。代价 = 空间单调增长(memory 规模可接受);真要 GDPR 式硬删,将来加定点操作(YAGNI)。

### 6.6 rebuild / repair

- `rebuild()`:清空所有物理表,按 `ds` 顺序 replay 全部 `<表>.jsonl` → 重新 embed + INSERT 行 + 对墓碑 UPDATE 软删 + 重建 vss/fts。派生层「表丢了能重建」是一个内建动作。
- `repair`(open 时轻量自检):file ≥ DuckDB 不变式若被 crash 打破(文件有、行没有),从文件补行(重新 embed + INSERT)。

---

## 7. 测试策略

**组织:按场景**(照 memory.talk)——每个场景一个目录(`tests/<name>/`),内含 `README.md`(测什么 / 不测什么 / fixture 来源)+ `test.py`;共享 fixture/helper 收在 `tests/conftest.py`(`db` / `pair` / `open_db` / `FakeEmbedder`)。已落场景:`quickstart` / `read_write` / `file_mirror` / `search` / `insert_only` / `time_machine` / `schema` / `server` / `embedder_live`。

- **契约测试**(核心):黑盒打端口——open→insert→query/search→delete→as-of→rebuild,断言 `file ≥ row ≥ vector` 与时光机语义。用一个**假 embedder**(确定性 hash→向量,零依赖),不碰真模型。
- **崩溃/重放**:在写入各步之间人为中断,重开断言收敛(file ≥ DuckDB + repair)。
- **planner**:构造下推/非下推谓词混合链,断言不犯 post-filter 返空病、排序语义。
- **并发**:并发写 + 前台读 + file 面 grep 并行,断言无锁读到完整文件、无半截 JSON。
- **两形态一致性**:同一条链分别走嵌入与 server(用 httpx `ASGITransport` 在进程内打全链路),断言结果一致、错误保型过线、as-of 只读闸在 HTTP 上也生效。
- **`ApiEmbedder` 冒烟**:mock 掉 httpx 端点断言批量/重试/维度;真端点用例标记跳过(需 key)。
- 工具:`pytest` + `pytest-asyncio`;每测试一个临时 `data_dir`(tmp_path)。

---

## 8. 分期实现(v1 全做,但有内部里程碑)

| 里程碑 | 内容 | 产出可用性 |
|---|---|---|
| **M1 骨架 + 结构化 + 两形态 ✅** | 包骨架、pyproject、schema 解析(list 形态)、DuckdbEngine、SQL `query`、ticket 写(insert/delete)、async 桥;**执行器抽象 + server 形态(open/connect,ASGI app,HTTP client)** | 嵌入 + server 两形态都能用 |
| **M2 文件镜像 ✅** | FileMirror(按天分区 / 每表 jsonl append)、文件最先写序、`rebuild` replay | file-canonical 立住 |
| **M3 检索 + search ✅** | 业务表上的 `vss`+`fts`、inline embed、SQL 里的 `search()`→**hybrid RRF**→`_score` join | 语义 + 全文查询上线 |
| **M4 时光机 ✅** | `ds`/`deleted_ds` 可见性谓词、`ds_start`/`ds_end` 裁剪、无物理删(历史永久) | 时光机严谨 |
| **M5 单引擎 ✅** | 砍掉 LanceDB,向量用 `vss`/HNSW、全文 `fts`/BM25、中文 jieba、RRF 融合;单文件、fd 恒定,去掉 EMFILE 恢复机械 | 单引擎、fd 无忧 |
| **M6 单表 + 同步写 ✅** | 塌成每业务表一张物理表(**写一次 PK**、软删)、insert 内联 embed 同步落库(去掉 outbox/consumer/事件日志)、per-col BM25 via `fields` | 结构最简、无异步兑现 |
| **M7 打磨(待续)** | `repair` open 时自检、`_meta` schema/dim 指纹、读写分连接、HNSW `compact_index` 维护、日内文件轮转 | 可发 PyPI |

M1–M6 核心已落(含单引擎 + 单表同步写);M7 是持续打磨项(§10 待定)。

---

## 9. 两种使用形态:嵌入 与 server(都做)

seekbase 是**一等支持的两种形态**,共用同一个 `Seekbase` 端口——调用方代码逐字节相同,只有拿 `db` 句柄的方式不同:

```python
db = await Seekbase.open(data_dir, schema=…, embedder=…)      # 嵌入:进程内、DuckDB
db = await Seekbase.connect(url, api_key=…)                   # server:同一端口走 HTTP
```

- **service 是唯一的用例入口,两个调用方都直连它**:HTTP 形态 `api/*.py` 直接调 `db.services.{query,write,admin,tickets}`(service 自己拼完整响应 + ticket);嵌入/远程形态走 `port → executor`,`LocalExecutor` 是**薄转发**(`op→service`,只为让 port 传输无关),`HttpExecutor` 把 `Request` 序列化成 HTTP 打到那些 api。两条路复用同一批 service 方法,零重复。分层:`{api 直连 | port→executor} → service(编排)→ _engine(机制:duck/search/files)`。
- **server 极简、无框架**(`server.py` + `api/`):`server.py` 是手写 ASGI 壳(auth + 收发 + 错误映射),路由与每个 endpoint 的逻辑拆进 `seekbase/api/` —— **一类接口一个文件**(`query/insert/delete/writes/rebuild/health.py`),目录即接口清单,和 `docs/api/*.md` 一一对应。`POST /v1/{query,insert,delete,rebuild}` + `GET /v1/{writes/{ticket},health}`。**server 标配 = `seekbase_server(db)`,零第三方依赖**;跑它的 **ASGI runner(uvicorn/hypercorn/…)由外部注入**——挂进自己的服务、或用便捷函数 `serve(db, runner=…)`(`runner` 默认取 uvicorn,前提是宿主装了)。client 端只需 httpx(核心已带)。测试用 httpx 的 in-process `ASGITransport` 打全链路,不需任何 runner。
- **错误保型过线**:server 侧抛的异常按类型映射 HTTP 状态码(`_wire.py`),client 侧重建同类型异常——`ReadOnlyError` 过 HTTP 还是 `ReadOnlyError`。
- **时间窗 per-request**:`ds_start`/`ds_end` 是 `query` 的参数(不绑连接),一个 server 进程能同时服务各自时间窗的多个 client;非只读语句被 `ReadOnlyError` 挡(权威判定在引擎,两形态同规矩)。
- **auth**:单个可选 bearer token;多租户 auth 非目标(§8)。
- **端口纪律**(两形态能共用的前提):**不塞「只有进程内才成立」的假设**——不漏 DuckDB 句柄、不假设 client 摸得到 `data_dir`、`query`/写 ticket/时光机语义在 HTTP 上也说得通。§4 公共面按此设计。

---

## 10. 待定(工程层)

- **返回类型**:`query` 现在返回纯 dict 行;要不要提供可选的行→模型绑定(如 pydantic)是个后续选项。
- **本地 embedder(TODO)**:`seekbase[st]` + `SentenceTransformerEmbedder`(本地模型,离线可用),同一 Embedder 协议;v1 只做 `ApiEmbedder`,本地模式后续补。
- **DuckDB 并发 / 读写分连接**:单连接串行是否够;前台读走独立只读连接以避开 FTS 重建期排队(见 [works/search.md](docs/works/search.md));并发读走多短读连接的收益/复杂度。
- **HNSW 维护**:删除留墓碑,长期 `PRAGMA hnsw_compact_index` 压缩的触发策略;HNSW 落盘 `hnsw_enable_experimental_persistence` 的 experimental 标签跟进。
- **进阶 searchable**:跨表 search、RRF 权重可调(`alpha`)。(**已做**:hybrid vss+fts + 多列 search,每个 searchable 列各自一套 vss+fts,`search(列, '文本')`。)
- **schema 演进**:`_meta` 指纹不符时——in-place migration(searchbase `AdminBackend` 那套)还是「实例=版本」蓝绿?dim 变更触发 reembed。
- **时光机细节**:用谁的钟、粒度(天够不够)、同日创删的边界。
- **写入吞吐**:insert 内联 embed(网络往返)+ 同步 FTS 重建的成本;批量 insert 的攒批、大表 FTS 重建的增量化。

---

## 11. 一句话

seekbase 作为 pip 包 = **一个 `Seekbase` 类背后的单引擎一目录**:DuckDB 一库通吃结构化 + 向量(`vss`/HNSW)+ 全文(`fts`/BM25),每业务表一张物理表(写一次、向量随行、软删),文件镜像管可审计,时光机靠 ds 分区兜底——单文件让 fd 恒定、去掉 LanceDB 的 EMFILE 之痛,对外只露 supabase 式链式查询 + hybrid `search()` 算子,`pip install seekbase` 即得。
