# architecture — 分层与调用链

> 状态:**已落**(M1:`PipelineService` 编译器 + `operator/` 包 + 可插拔检索后端)。seekbase 的代码分层与一次调用怎么流过它们。**读是一根管道**(SPL 式,`stage | stage`,见 [pipeline-as-anything.md](pipeline-as-anything.md)):检索是一个 source 段、transform 段是原生 DuckDB SQL、算子段跳出 DuckDB。**结构化 DuckDB + 可插拔检索后端**(lance / duck-vss)+ 文件镜像;两种使用形态(嵌入 / HTTP)共用同一套 service。
>
> **和现网代码的差异**:现网读路径是 `ReadService` + rewrite 层(`extract_searches`/`search_target`/缝合)+ 单条 SQL,检索焊死在 DuckDB-vss;本文按管道方向把读路径换成**管道编译器**,rewrite 层退休、检索引擎可插拔。写路径(WriteService worker / ticket / files-first)基本不变。

## 1. 一切皆 service

`_engine/` 已化掉。除了入口(`client.py`/`server.py`)、数据(`struct/`)和基础设施(`runtime/`),其余都是 **service**——分两类:**领域服务**各自拥有一个子域端到端,**用例服务**只做薄编排(跨子域的顺序 + 原子)。

```
client.py / server.py    两形态入口:open(嵌入)/ connect(远程) / seekbase_server(HTTP)
  │
  ├── api/               HTTP 协议两半:端点(query/insert/…)+ remote.py(HttpExecutor 客户端)
  │      直接调 →
  └── client.py       LocalExecutor:op→service 薄转发(让 client 传输无关)
         │
         ▼
      service/  ── 用例服务(薄编排):pipeline(读)· write · admin(+ tickets)
                └ 领域服务(拥有子域):store(结构化 DuckDB)· search(可插拔检索后端)· embedding(文本→向量/token)· files(镜像)
         │
         ▼
      struct/            贯穿各层的数据对象:Request · Ticket · Schema/TableSpec/Column · Row/Hit · Pipeline/Stage
      runtime/           基础设施:bridge(单写者)· clock(ds/created_at)
```

- **领域服务拥有子域**:`FileService` 拥有落盘记录/墓碑的形状,`StoreService` 拥有结构化 DuckDB(过滤/聚合/join/`ds` 时间窗 + 列校验),`SearchService` 拥有**可插拔检索后端**(lance / duck-vss,喂 `search` source 段),`EmbeddingService` 拥有 embed+分词。
- **用例服务是薄编排**:`WriteService.insert` 只有几行,唯一职责是跨子域的**顺序+原子**(files 先于派生层);`PipelineService` 把一根管道**编译 + 逐段执行**(§2)——领域服务管不了这个(它们不知道别的子域)。
- **service 是唯一用例入口**:HTTP 走 `api/*.py` **直连** `db.services.*`;嵌入/远程走 `client → executor`,`LocalExecutor` 只把 `Request` 的 op 转发到同一批 service。两条路复用同一批 service,零重复。
- **检索引擎藏在 `SearchService` 背后**:它对上只承诺产一张 `(pk, score)` 表(见 [search.md §2](search.md));背后是 LanceDB 还是 DuckDB-vss,`PipelineService` 不关心。

## 2. 一次读(管道)怎么流

读是一根管道:`search cards "pty 终端" | SELECT * FROM _in WHERE kind='issue' ORDER BY _score DESC LIMIT 20`。

```
Seekbase.query(pipeline)                              # client.py:构造 Request(op=query,载荷=管道串)
  → LocalExecutor.execute                             # client.py(LocalExecutor):op→service
    → PipelineService.run                            # service/pipeline_service.py:薄编排
        parse(pipeline)                               #   按 | 切段;每段看首 token:命中 registry→算子,否则当 SQL(缺省)
        for stage in stages:  fold _in                #   逐段重绑 _in
          ├─ source: search →                         #   EmbeddingService.embed+tok → SearchService.hybrid(表,列,向量,token)
          │                                           #     → 物化成 _in(pk, _score, …)
          ├─ SQL(缺省,首 token 不命中)→              #   StoreService.run_sql(sql, _in):只读守卫 + 可见性视图
          └─ external: sh/http/grep →                     #   registry 命中 + 策略放行 → 序列化 _in → 子进程 → 回 _in
  → {"rows": [...]}                                    # 末段 _in 逐层原样返回
```

- **相邻两段都在 duck 里就该合成一条 SQL**(pipeline-as-anything §2.1)——`|` 只在跨引擎/跨进程的接缝出现。纯 SQL query 有零个 `|`,直接 `StoreService.run_sql`,`PipelineService` 不插手。
- **rewrite 层退休**:不再有 `extract_searches`/`search_target`/缝合——检索是 source 段、产物是真表,SQL 从不需要被抠开再缝回。
- HTTP 形态:`connect` 的 `HttpExecutor` 把 `Request` 序列化打到 `api/query.py`,后者调**同一个** `PipelineService.run`——即上图从 PipelineService 起完全一致。

## 3. 一次写(`insert`)怎么流

```
Seekbase.insert(table, rows)                          # client.py → Request(op=insert)
  → LocalExecutor.execute → WriteService.insert       # service/write_service.py:薄编排(只管顺序+原子)
        StoreService.validate(...)                    #   store 拥有:列校验 + dup-pk(PK 约束兜底)
        EmbeddingService.embed_records(...)           #   embedding 拥有:内联 embed + jieba 分词
        FileService.write_puts(...)                   #   file 拥有落盘形状;文件最先(canonical)
        StoreService.commit_rows(...)                 #   结构化行 INSERT
        SearchService.index(...)                      #   喂检索后端:duck-vss 随行写 _vec/_tok + FTS 重建,或 lance append
        TicketService.issue("insert")                #   → struct.Ticket
  → Ticket                                            # client.insert 取 .id;api 出口 .to_wire()
```

写是**同步**的:`insert` 返回即结构化行 + 检索索引都已落库、可搜;ticket 恒 `done`。删除同理,`WriteService.delete` 软删(`UPDATE deleted_ds`),检索侧靠查询时的 as-of 谓词裁掉(见 [search.md §6](search.md))。

## 4. 关键接缝

- **两形态接缝 = `Request` + executor**:`client` 只构造 `Request`(读的载荷是管道串),不认识本地/远程;`LocalExecutor`→service,`HttpExecutor`(`api/remote.py`)→HTTP。`Ticket` 在 HTTP 边界 `to_wire`/`from_wire`,client 本地/远程拿到的都是 `Ticket`,传输无关。
- **管道接缝 = `_in` 表(stage ABI)**:段与段之间只交换一张关系,恒名 `_in`。DuckDB 段间零拷(temp view),算子段跨进程才序列化(Arrow/JSONL)。检索引擎的接缝也在这里——`SearchService` 产表、下游 SQL 读表。
- **单写者 bridge**(`runtime/bridge.py`):一个单线程持有唯一 DuckDB 连接,所有 DuckDB 操作串行化。读走 ReadPool 的 cursor(MVCC 并发,不排在写后,见 [concurrency.md](concurrency.md))。
- **canonical 是文件**:`StoreService` 的 DuckDB 和 `SearchService` 的检索后端都是从文件可重建的**派生层**;`AdminService.rebuild` 重放镜像重灌两者(见 [store.md](store.md))。

## 5. 与其他文档

- [pipeline-as-anything.md](pipeline-as-anything.md):读为什么是管道、`_in` 表 ABI、`search()` UDF 为何退休、SQL 是缺省。
- [operator-registry.md](operator-registry.md):`PipelineService` 编译期查的 operator registry + 能力/策略/沙箱权限围栏。
- [store.md](store.md):两层存储(files canonical / 派生 = 结构化 DuckDB + 检索后端)、rebuild、一致性。
- [search.md](search.md):`search` source 段 + 可插拔引擎(lance / duck-vss)+ RRF + jieba。
- [time_machine.md](time_machine.md):`ds`/`deleted_ds` 可见性谓词,作为 source 段入参。
- [schema.md](schema.md):`searchable` 如何接线检索后端。
- [../../DESIGN.md](../../DESIGN.md):完整工程设计(边界、依赖、分期)。
