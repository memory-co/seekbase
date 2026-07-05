# seekbase — pip 包设计

> 把 [seekbase v5 设计稿](../memory.talk/docs/works/v5/seekbase.md)（概念层）落成一个**独立、可 `pip install` 的库**的工程设计。
>
> 概念不变:一个数据端口 = 类 supabase 的 ORM + `search()` 一等算子,底层 DuckDB + LanceDB + 文件镜像,焊死 insert-only,自带 outbox 与时光机。本文只讲**怎么把它做成包**:边界、依赖、目录、公共契约、内部架构、并发模型、测试、分期。
>
> **v1 范围决策(已定):完整愿景全做** · **embedder 纯注入 + 可选 `[st]` extra** · **async 优先**。

---

## 0. TL;DR

- 包名 `seekbase`,`import seekbase` / `from seekbase import Seekbase`。Python **3.11+**,Apache-2.0(仓库已有 LICENSE)。
- 运行时依赖:`duckdb`、`lancedb`、`pydantic>=2`、`httpx`。**两种形态都是标配、不开任何 extra**——嵌入、HTTP 客户端(`connect`,靠 httpx)、把端口暴露成服务的 `seekbase_server(db)`(**零依赖手写 ASGI app**)。跑 app 的 **ASGI runner(uvicorn/hypercorn/…)由宿主外部注入**,不是 seekbase 依赖(和 Starlette/FastAPI 同路数:库给 app,runner 自带)。embedder 走**纯注入**,默认实现 `ApiEmbedder`(OpenAI 兼容 `/embeddings`)也是核心一部分,核心仍**不加载任何本地模型**。本地 sentence-transformers 模式**记 TODO 后续做**(§10)。
- **业务无关**(继承 searchbase 纪律):包里不出现 card/round/session,不读任何 Config,只收明确的值(`data_dir` / `schema` / `embedder`)。
- 公共面就一个类 `Seekbase` + 一个链式 `QueryBuilder` + 几个值类型/协议。引擎(DuckDB/Lance/文件/outbox/planner/时光机)全在 `_engine/` 后面,不导出。
- 一个 seekbase **实例 = 一个目录**:`<data_dir>/{duck.db, lance/, files/, _meta.json}`。拷走目录 = 拷走整个库。

---

## 1. 包身份与边界

| 维度 | 取值 | 说明 |
|---|---|---|
| PyPI 名 | `seekbase` | import 名同名 |
| Python | `>=3.11` | 现代 typing、`asyncio.TaskGroup`、`tomllib` |
| License | Apache-2.0 | 仓库已带 |
| 形态 | 进程内嵌入库(async) | 云端版是**形态预留**,不实现(见 §9) |
| 依赖它的人 | 宿主应用的组装根 | 由宿主注入 embedder / 路径;seekbase 不认识业务、不读 Config |

**包的对外承诺(端口契约)**:调用方只见 ORM 链 + `search()` + 原始 SQL + 少数管理动作(`flush/rebuild/vacuum/close`)。**永远看不见**:向量、embedding 计算、outbox、consumer、DuckDB 连接句柄、Lance 目录结构、文件落盘顺序。这条「不漏进程内假设」的纪律也是云端版能直接换 HTTP 的前提(§9)。

---

## 2. 依赖与打包

```toml
# pyproject.toml (hatchling, flat layout)
[project]
name = "seekbase"
requires-python = ">=3.11"
dependencies = [
    "duckdb>=1.1",       # 结构化/分析引擎,原生 read_json/parquet
    "lancedb>=0.13",     # 向量引擎(吸收 searchbase 的 local 实现)
    "pydantic>=2",       # 值类型 + schema 校验(与 searchbase 一致)
    "httpx>=0.27",       # HTTP 客户端(connect)+ 默认 embedder(ApiEmbedder)
    # server 标配靠 seekbase_server()(零依赖 ASGI app);ASGI runner 由宿主外部注入,不绑定
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "ruff", "mypy"]
# TODO: st = ["sentence-transformers>=3"]  # 本地模型 embedder,后续做(§10)

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- **pyarrow** 由 duckdb/lancedb 传递带入,不直接声明(两者同宗 Arrow 生态,零拷贝互通是 §3 的底子)。
- 默认 embedder 只需一个轻量 HTTP 客户端(`httpx`,已进核心依赖),不碰任何模型权重;`pip install seekbase` 装完不下载任何权重。**本地 `st` 模式暂不做,记 TODO(§10)。**
- 无 numpy 直接依赖(向量以 `list[float]` / Arrow 进出;若内部要 ndarray 也由 lancedb 带)。

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
    _types.py                  # Row/Hit、Embedder 协议、错误层级 —— 纯值类型
    port.py                    # Seekbase(async 门面,open/connect)+ QueryBuilder(链式)
    schema.py                  # SCHEMA 解析/校验 → 内部 TableSpec(columns/searchable/files)
    server.py                  # 手写 ASGI app(seekbase_server / serve)—— server 形态(§9)  [M1 已落]
    _wire.py                   # Request 序列化 + 错误↔HTTP 状态码映射(client/server 共用)  [M1 已落]
    _engine/
      plan.py                  # Predicate / Plan / Request —— 传输中立的查询原语  [M1 已落]
      executor.py              # LocalExecutor(→DuckDB)/ HttpExecutor(→HTTP),两形态的接缝  [M1 已落]
      duck.py                  # DuckdbEngine:单写者连接、DDL、SQL 编译执行、as-of 可见性
      bridge.py                # async↔sync 桥(单线程 executor,串行化 DuckDB)  [M1 已落]
      vector.py                # VectorEngine:LanceDB 管理(吸收 searchbase.local)  [M3]
      files.py                 # FileMirror:json / jsonl 三写、原子落盘、read_json 桥  [M2]
      outbox.py                # Outbox(DuckDB 表)+ Consumer(进程内协程)  [M3]
      planner.py               # 查询规划:search()+谓词组合、过滤下推、as-of 改写  [M3]
      asof.py                  # 时光机:ds 日期分区裁剪 / 只读闸  [M4]
    embedders/
      __init__.py              # Embedder 协议再导出
      api.py                   # 默认 ApiEmbedder(OpenAI 兼容 /embeddings,async httpx,核心自带)  [M1 已落]
      # TODO: sentence_transformer.py  # 本地模型 embedder,后续做(§10)
  tests/                       # 按场景组织(照 memory.talk):每个子目录 = 一个场景
    conftest.py                # 共享 fixture/helper:db / pair / open_db / FakeEmbedder
    README.md                  # 场景一览表 + 加新场景的规矩
    basic_orm/                 # 核心结构化读写 round-trip(README.md + test.py)
    insert_only/               # delete 只打墓碑、无 update 路径
    time_machine/              # as-of 回退 + 只读闸(嵌入)
    schema/                    # SCHEMA 校验 + 未知列拒 + searchable 需 embedder
    server/                    # server 形态:同一条链走 HTTP(in-process ASGITransport)
```

> **测试组织照 memory.talk 的「按场景」路数**:每个场景一个目录,内含 `README.md`(测什么 / 不测什么 / fixture 来源)+ `test.py`;`python_files` 收 `test.py`。相关用例合并到一个场景下,跟「按代码模块切文件」解耦。

**与 searchbase 的映射**:`searchbase/local/{backend,index,maintenance,util}.py` 那摊(embed、ANN、auto_split、超长截断、压缩/EMFILE 恢复、维护协程)**整体下沉**为 `_engine/vector.py`(可拆子模块)。searchbase 的端口 `SearchBackend` **不再对外**——上层只 import `seekbase.Seekbase`。

---

## 4. 公共契约(端口)

### 4.1 打开 / 关闭

```python
from seekbase import Seekbase

db = await Seekbase.open(
    data_dir,                 # Path:实例目录(自动建 duck.db / lance/ / files/)
    schema=SCHEMA,            # 声明式表结构(§4.5)
    embedder=embedder,        # 任何满足 Embedder 协议的对象;无 searchable 列时可省
    as_of=None,               # None=当前态(可写);给时间点=只读时光机(§7)
)
...
await db.close()             # 排干 outbox、停 consumer、关连接
```

- **async 类工厂**(照 searchbase `create`):开库 + 起 consumer 协程都要 await。
- `db.ready` (property):底层可用性;`False` → 宿主应回 503 / 降级。
- 上下文管理器糖:`async with Seekbase.open(...) as db:`。

### 4.2 ORM 链(supabase 那部分,砍掉修改类)

```python
rows = await (db.table("cards")
    .select("card_id", "issue", "created_at")   # 省略=select *
    .eq("kind", "issue").gte("created_at", "2026-06-01")
    .order("created_at", desc=True).limit(20))   # await 触发执行 → list[dict]

await db.table("cards").insert({"card_id": "c1", "issue": "…"})   # 也可 list[dict] 批量
await db.table("cards").delete().eq("card_id", "c1")             # 仅打墓碑,永不物理删
```

- 算子表:`select / insert / delete` + `eq neq gt gte lt lte in_ like ilike is_ order limit offset count`。
- **`QueryBuilder` 是惰性的、可 `await` 的**:`__await__` 触发规划+执行。`insert()/delete()` 同样返回 awaitable。链上不可变、每步返回新 builder(便于复用前缀)。
- **焊死不变性(引擎强制,非纪律)**:端口无 `update`/`upsert`;`delete()` 唯一语义是写 `deleted_at` 墓碑。「改」= 追加新行(旧行墓碑)或落 append-only 事件表 + 视图现算。
- 返回:v1 **纯 dict 行**。`select_as(Model)` 的可选 pydantic 绑定列入 §10 待定,不进 v1 表面。

### 4.3 `search()` 一等算子

```python
hits = await (db.table("cards")
    .search("为什么 pty 会让用户想到 tmux")   # 自动 embed + 向量检索
    .eq("kind", "issue").gte("created_at", "2026-06-01")  # 同一条链上的结构化过滤
    .limit(10))
# hits: list[dict],结构化列齐全 + 附 `_score`,默认按语义相关排序
```

- `search(text, *, mode="semantic")` —— `mode="hybrid"` 的位留着(§10),v1 只纯向量。
- 调用方**永不见向量、不算 embedding**(继承 searchbase)。组合执行策略见 §6.3。

### 4.4 SQL 面 + 管理动作

```python
rows = await db.sql("SELECT kind, count(*) FROM cards GROUP BY kind")  # 只读直查(as-of 下自动回溯)
await db.flush()             # 排干 outbox → 读己之写(search 立即可见)
await db.rebuild()           # 通读 files 声明 → 重灌 DuckDB + LanceDB(派生层可重建)
await db.vacuum(before=T)    # 显式丢历史:物理清 T 前墓碑(行 + 向量 + 文件)
```

- `sql()` 是**只读逃生舱**(join/聚合/窗口/对账);写只能走 ORM(保住 insert-only + 三写不变性)。
- `flush()` 的语义在 HTTP 上也说得通(§9):它是「等最终一致收敛到强一致」,不暴露 outbox。

### 4.5 声明式 SCHEMA

```python
SCHEMA = {
    "cards": {
        "columns": {"card_id": "str primary", "issue": "str",
                    "kind": "str", "created_at": "str"},
        "searchable": ["issue"],                 # 这列可 search():写入自动 embed,search 自动查
        "files": "cards/{card_id}.json",         # 本地 JSON 镜像(可 grep)
    },
    "rounds": {
        "columns": {"session_id": "str", "idx": "int", "text": "str"},
        "searchable": ["text"],
        "files": {"path": "sessions/{session_id}/rounds.jsonl", "mode": "jsonl"},
    },
}
```

- `columns` 类型:`str/int/float/bool` + 修饰 `primary`(每表恰一主键,做 id 对齐)。**声明式、不从首行推断**(避免 null→错判列型,searchbase 已踩过)。
- `created_at` / `deleted_at` 是**引擎代管的元数据列**:schema 没写也自动加(时光机与墓碑靠它,§7)。
- `searchable` = 「search 自动模糊查询」的开关:声明了 → insert 自动 embed 进向量侧、search 自动查;没声明的表就是**纯 DuckDB 表,零向量开销**。
- `files` 缺省 = 无镜像(纯派生/日志表不必落盘)。
- **schema 校验在 open 时做一次**:主键唯一、searchable/files 列存在、模板占位符都是真实列、embedder 存在性(有 searchable 列却没给 embedder → 明确报错)。

### 4.6 值类型与协议(`_types.py`)

```python
class Embedder(Protocol):            # 注入契约(sync 或 async 都收)
    @property
    def dim(self) -> int: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...   # 可返回 awaitable

Row  = dict                          # 普通 dict 行(v1)
Hit  = dict                          # 同 Row,多一个 "_score": float

# 错误层级
class SeekbaseError(Exception): ...
class SeekbaseUnavailable(SeekbaseError): ...   # 底层开不了 → 宿主回 503
class SchemaError(SeekbaseError): ...           # open 时 schema 校验失败
class EmbedderInvalid(SeekbaseError): ...       # embedder 维度/契约不符
class ReadOnlyError(SeekbaseError): ...         # 往时光机连接写
```

- Embedder 协议**同时容忍 sync/async**:内部 `await maybe_await(embedder.embed(...))`(检测 coroutine)。
- **v1 默认 `ApiEmbedder`(核心自带,开箱即用)**:async httpx 调 OpenAI 兼容 `/embeddings`,构造收 `base_url / api_key / model / dim`,内部批量 + 退避重试。端口仍只认 `Embedder` 协议(可换任意注入实现),但默认这一个进核心、不另开 extra。
- **本地 sentence-transformers embedder = TODO**(§10):后续加 `seekbase[st]` + `SentenceTransformerEmbedder`,同一协议,零改端口。

---

## 5. 实例布局(磁盘)

```
<data_dir>/                       # 一个 Seekbase 实例 = 一个目录(拷走=完整备份)
  duck.db                         # DuckDB 单文件:业务行 + _outbox 表 + _meta
  lance/                          # LanceDB:每个有 searchable 列的表一个 collection
  files/                          # 文件镜像(canonical):顶层按 ds=YYYYMMDD 日期分区,内 cards/*.json 等
  _meta.json                      # 实例元:schema 指纹、seekbase 版本、embedder dim
```

- `_meta.json` 记 schema 指纹与 embedder dim:**open 时比对**——schema 变更或 dim 变更给出明确升级路径(§10;searchbase「实例=版本」的蓝绿思路可复用)。
- 三个引擎共处一个目录,对齐 searchbase 的 `name="v1"` 实例化路数。

---

## 6. 内部架构

### 6.1 三引擎一端口(§3 概念的落地)

```
              Seekbase(port.py:门面 + QueryBuilder)
                            │  planner.py 规划
        ┌───────────────────┼───────────────────┐
   FileMirror           DuckdbEngine         VectorEngine
   (files.py)            (duck.py)            (vector.py, 吸收 searchbase)
   json/jsonl 三写        行·过滤·聚合·join      embed·ANN·auto_split·压缩
        └───────── Outbox + Consumer(outbox.py)对齐 ─────────┘
```

### 6.2 写入流水与一致性(file ≥ row ≥ vector)

`insert()` 三步,顺序焊死:

```
① FileMirror 写 JSON/JSONL(temp+rename 原子落盘)          ← canonical 先落地
② 一个 DuckDB 事务:写业务行 + 追加 _outbox 一行(向量作业)  ← 原子(队列就在 DuckDB 里)
③ Consumer 异步:取 pending → embed → Lance upsert → 标 done ← 最终一致
```

- **跨引擎无事务** → 用 transactional **outbox**;巧处是队列表 `_outbox` 就在 DuckDB 里,「业务写 + 入队」天然同一事务,原子性不出引擎。
- **at-least-once + 幂等**:consumer 可能重放(标 done 前崩),但向量按 id upsert/delete 幂等,重放无害;不需恰好一次。
- **崩溃恢复 = 重放**:pending 与业务行同事务落盘,重启从 pending 续跑;彻底丢了还能 `rebuild()` 从文件整体重建。
- **一致性关系固定可推理**:`file ≥ row ≥ vector`。file 面永不缺数据(至多瞬时超前 row 一步,crash 后 repair 收敛);row(不带 search 的查询)强一致;vector 最终一致。要读己之写 → `flush()`。
- `delete()` 同路:一个 DuckDB 事务里给行打墓碑 + 入队向量删除作业;文件侧把 `deleted_at` 写回 JSON(json 唯一一次重写)/ 给 jsonl 追加墓碑行。

### 6.3 planner:search() 与谓词组合

```
无 search()  → 纯 DuckDB:谓词→WHERE、order/limit/offset→SQL,一次查询(不碰向量)
有 search()  → ① 可下推谓词(eq/范围,列在 Lance fields 里)翻成 Lance filter,pre-filter 下推
                 → 在过滤后的子集上 ANN,保「先过滤后取 top-k」(不犯 post-filter 返空的病)
               ② 下推不了的(join/复杂表达式):向量侧取放大候选(k×系数)→ 回 DuckDB 精过滤+补列 → 截 limit
               ③ 排序:默认按 _score;显式 order() 则按指定列(_score 仍附行上)
```

- **as-of 谓词也走 pre-filter 下推**(§7 机制复用):检索「当时存在的向量」。
- 需要下推的字段:planner 让 VectorEngine 在 Lance collection 里把这些标量列存成 fields(`created_at`/`deleted_at` + schema 里被用作过滤的列)。

### 6.4 async↔sync 桥与并发(`_bridge.py`)

- DuckDB/LanceDB 本身同步。**单写者模型**:一个专属**单线程 executor** 持有 DuckDB 写连接,所有 DuckDB 操作在该线程串行化(避免跨线程连接 + 满足 DuckDB 单写者)。
- Consumer 是 asyncio 协程:它的 DuckDB 读写也排进同一 executor(与前台写串行,天然不打架);embedding + Lance 写走各自路径(embedding 可能是 async;Lance 写另有 executor)。
- **file 面无锁、不经 executor**:`grep`/`cat`/`diff` 直接读文件树,不占 DuckDB 连接、不被写入阻塞(靠 insert-only + 原子落盘兜底)。
- v1 单连接串行化优先做对;并发读优化(多短读连接)列 §10。

### 6.5 时光机 = 日期分区(`ds`)

时光机用**离线大数据那套分区**实现,不靠谓词改写。**完整设计(创建/删除两对日期字段、可见性谓词、完备性证明、vacuum 语义)见 [works/time_machine.md](docs/works/time_machine.md)**;这里给要点。

- **两对引擎代管日期字段**:创建 `(ds, created_at)` + 删除 `(deleted_ds, deleted_at)`;`_ds` 是天(`YYYYMMDD`,分区/时光机判定),`_at` 是精确时刻。**只有创建日不够**——判断「as-of D 时该行是否已删」必须有删除日 `deleted_ds`(否则早创建、晚删除的行会被错判)。
- **可见性谓词**:as-of D = `ds <= D AND (deleted_ds IS NULL OR deleted_ds > D)`。`ds <= D` 是分区裁剪(扫描量随时间窗收敛);`deleted_ds` 那半句管删除回溯。
- **机制 = 分区裁剪**:`query` 带 `ds_start` / `ds_end`(闭区间)。只给 `ds_end` = 时光机(as-of ds_end);只给 `ds_start` = 那天之后仍活;两个都给 = 该窗口创建、且 ds_end 时仍活。时间窗是**查询参数、不绑连接**。
- **文件即分区**:文件镜像用 `ds=YYYYMMDD` 做**顶层目录**;insert 事件落创建日分区、delete 墓碑落删除日分区,`ls files/ds=20260705/` = 当天发生的事(建的行 + 删的墓碑)。
- **严谨性靠 insert-only + 分区**:文件纯 append、永不回改;派生 DuckDB 行的 `deleted_ds` 由消费墓碑事件置上(派生可改、canonical 不改)。
- 代价:历史常驻 = 空间换历史;`vacuum(before=D)` = **按行**清 `deleted_ds < D` 的死行(创建文件+墓碑+行+向量),**不是**整块删分区(会误删仍活的老行);活行与删于 `≥D` 的行都保留。

### 6.6 rebuild / repair

- `rebuild()`:读 `files` 声明的全部文件(DuckDB 原生 `read_json`/glob 当外部表)→ 重灌 DuckDB 行 + 重新入队全部向量作业。派生层「表丢了能重建」从各 store 手写变成一个内建动作。
- `repair`(open 时轻量自检):file ≥ row 不变式若被 crash 打破(文件有、行没有),从文件补行 + 补 outbox;vector 缺失由 outbox replay 自愈。

---

## 7. 测试策略

**组织:按场景**(照 memory.talk)——每个场景一个目录(`tests/<name>/`),内含 `README.md`(测什么 / 不测什么 / fixture 来源)+ `test.py`;共享 fixture/helper 收在 `tests/conftest.py`(`db` / `pair` / `open_db` / `FakeEmbedder`)。M1 已落五个场景:`basic_orm` / `insert_only` / `time_machine` / `schema` / `server`。

- **契约测试**(核心):黑盒打端口——open→insert→select/search→delete→as-of→flush→rebuild→vacuum,断言 `file ≥ row ≥ vector` 与时光机语义。用一个**假 embedder**(确定性 hash→向量,零依赖),不碰真模型。
- **崩溃/重放**:在 ①②③ 各步之间人为中断,重开断言收敛(outbox replay + repair)。
- **planner**:构造下推/非下推谓词混合链,断言不犯 post-filter 返空病、排序语义。
- **并发**:并发写 + 前台读 + file 面 grep 并行,断言无锁读到完整文件、无半截 JSON。
- **两形态一致性**:同一条链分别走嵌入与 server(用 httpx `ASGITransport` 在进程内打全链路),断言结果一致、错误保型过线、as-of 只读闸在 HTTP 上也生效。
- **`ApiEmbedder` 冒烟**:mock 掉 httpx 端点断言批量/重试/维度;真端点用例标记跳过(需 key)。
- 工具:`pytest` + `pytest-asyncio`;每测试一个临时 `data_dir`(tmp_path)。

---

## 8. 分期实现(v1 全做,但有内部里程碑)

| 里程碑 | 内容 | 产出可用性 |
|---|---|---|
| **M1 骨架 + 结构化 + 两形态** | 包骨架、pyproject、schema 解析、DuckdbEngine、ORM(select/insert/delete/count)、SQL 直查、async 桥、部分 as-of;**执行器抽象 + server 形态(open/connect,ASGI app,HTTP client)** | 嵌入 + server 两形态都能用 |
| **M2 文件镜像** | FileMirror(json/jsonl、原子落盘、read_json 桥)、三写顺序、rebuild/repair | file-canonical 立住 |
| **M3 向量 + search** | 吸收 searchbase→VectorEngine、Outbox+Consumer、planner 下推、`search()`、`flush()` | 语义查询上线 |
| **M4 时光机** | `ds`/`deleted_ds` 日期分区、as-of 分区裁剪、vacuum(按行清死行) | 时光机严谨 |
| **M5 打磨** | `ApiEmbedder`(核心自带)、README、契约测试补全、错误信息、`_meta` schema 指纹 | 可发 PyPI |

一次交付 = M1→M5 全落;里程碑只为内部可验收切分。

---

## 9. 两种使用形态:嵌入 与 server(都做)

seekbase 是**一等支持的两种形态**,共用同一个 `Seekbase` 端口与 `QueryBuilder`——调用方代码逐字节相同,只有拿 `db` 句柄的方式不同:

```python
db = await Seekbase.open(data_dir, schema=…, embedder=…)      # 嵌入:进程内、DuckDB
db = await Seekbase.connect(url, api_key=…, as_of=…)          # server:同一端口走 HTTP
```

- **一个执行器抽象撑起两形态**(`_engine/executor.py`):`LocalExecutor` 把 `Request` 直接派给 DuckdbEngine;`HttpExecutor` 把同一个 `Request` 序列化成 `POST /v1/execute` 发给 server。`QueryBuilder` 只构造 `Request`,不认识自己跑在哪种形态。
- **server 极简、无框架**(`server.py`):手写 ASGI app,两条路由——`POST /v1/execute`(跑一个序列化的 `Request`)、`GET /v1/health`。**server 标配 = `seekbase_server(db)`,零第三方依赖**;跑它的 **ASGI runner(uvicorn/hypercorn/…)由外部注入**——挂进自己的服务、或用便捷函数 `serve(db, runner=…)`(`runner` 默认取 uvicorn,前提是宿主装了)。client 端只需 httpx(核心已带)。测试用 httpx 的 in-process `ASGITransport` 打全链路,不需任何 runner。
- **错误保型过线**:server 侧抛的异常按类型映射 HTTP 状态码(`_wire.py`),client 侧重建同类型异常——`ReadOnlyError` 过 HTTP 还是 `ReadOnlyError`。
- **as_of 是 per-request**:DuckdbEngine 的 as-of 从引擎级下沉成**每次调用的参数**,所以一个 server 进程能同时服务各自 `as_of` 的多个 client;写在 `as_of` 连接上一律被 `ReadOnlyError` 挡(权威判定在 `LocalExecutor`,两形态同规矩)。
- **auth**:单个可选 bearer token;多租户 auth 非目标(§8)。
- **端口纪律**(两形态能共用的前提):**不塞「只有进程内才成立」的假设**——不漏 DuckDB 句柄、不假设 client 摸得到 `data_dir`、`flush()`/`search()`/时光机语义在 HTTP 上也说得通。§4 公共面按此设计。

---

## 10. 待定(工程层)

- **ORM 定形**:可选 pydantic 绑定(`select_as(Model)`);多语句事务要不要暴露(跨引擎原子性已由 outbox 解决)。
- **本地 embedder(TODO)**:`seekbase[st]` + `SentenceTransformerEmbedder`(本地模型,离线可用),同一 Embedder 协议;v1 只做 `ApiEmbedder`,本地模式后续补。
- **hybrid search**:`mode="hybrid"`(向量+BM25/DuckDB FTS),融分(RRF?)何时做。
- **DuckDB 并发**:单连接串行是否够;并发读走多短读连接的收益/复杂度。
- **进阶 searchable**:多列 search、跨表 search(`db.search("…", tables=[…])`)。
- **schema 演进**:`_meta` 指纹不符时——in-place migration(searchbase `AdminBackend` 那套)还是「实例=版本」蓝绿?dim 变更触发 reembed。
- **时光机细节**:用谁的钟、粒度(秒够不够)、`as_of=T` 但向量还在队里的边界、vacuum 默认策略。
- **outbox 调度**:consumer 与前台写共用单连接的调度公平性;批量 embed 的攒批窗口。

---

## 11. 一句话

seekbase 作为 pip 包 = **一个 `Seekbase` 类背后的三引擎一目录**:DuckDB 管结构化、LanceDB 管语义、文件管可审计,outbox 缝合双引擎、时光机靠 insert-only 兜底——对外只露 supabase 式链式查询 + `search()` 算子,`pip install seekbase`(要开箱 embedder 再 `[st]`)即得。
