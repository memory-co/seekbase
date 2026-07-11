# architecture — 分层与调用链

> seekbase 的代码分层与一次调用怎么流过它们。单引擎(DuckDB: 结构化 + `vss` + `fts`)+ 文件镜像;两种使用形态(嵌入 / HTTP)共用同一套 service。

## 1. 一切皆 service

`_engine/` 已化掉。除了入口(`client.py`/`server.py`)、数据(`struct/`)和基础设施(`runtime/`),其余都是 **service**——分两类:**领域服务**各自拥有一个子域端到端,**用例服务**只做薄编排(跨子域的顺序 + 原子)。

```
client.py / server.py    两形态入口:open(嵌入)/ connect(远程) / seekbase_server(HTTP)
  │
  ├── api/               HTTP 协议两半:端点(query/insert/…)+ remote.py(HttpExecutor 客户端)
  │      直接调 →
  └── service/dispatch   LocalExecutor:op→service 薄转发(让 client 传输无关)
         │
         ▼
      service/  ── 用例服务(薄编排):query · write · admin(+ tickets · rewrite)
                └ 领域服务(拥有子域):store(DuckDB 结构化)· search(vss+fts)· files(镜像)
         │
         ▼
      struct/            贯穿各层的数据对象:Request · Ticket · Schema/TableSpec/Column · Row/Hit
      runtime/           基础设施:bridge(单写者)· clock(ds/created_at)
```

- **领域服务拥有子域**:`FileService` 拥有落盘记录/墓碑的形状,`StoreService` 拥有列校验+dup-pk,`SearchService` 拥有 embed+分词——子域知识不再漏进上层。
- **用例服务是薄编排**:`WriteService.insert` 只有 5 行,唯一职责是跨子域的**顺序+原子**(files 先于 db)——领域服务管不了这个(它不知道别的子域)。
- **service 是唯一用例入口**:HTTP 走 `api/*.py` **直连** `db.services.*`;嵌入/远程走 `client → executor`,`LocalExecutor` 只把 `Request` 的 op 转发到同一批 service。两条路复用同一批 service,零重复。
- `StoreService` 与 `SearchService` **共享同一条 DuckDB 连接**(单引擎):`commit_rows` 在一个 bridge 块里做 INSERT + FTS 重建。

## 2. 一次读(`search()` 查询)怎么流

```
Seekbase.query(sql)                                   # client.py:构造 Request(op=query)
  → LocalExecutor.execute                             # service/dispatch.py:op→service
    → QueryService.query                              # service/query.py:薄编排
        rewrite.extract_searches(sql)                 #   search(col,'x') → 占位 + specs
        rewrite.search_target(...)                    #   定表
        SearchService.hybrid(表,列,文本)             #   service/search.py:vss+fts RRF → [(pk,score)]
        StoreService.run_query(重写SQL, searches)     #   service/store.py:只读守卫 + 可见性视图 + join
  → {"rows": [...]}                                   # 逐层原样返回
```

HTTP 形态:`connect` 的 `HttpExecutor` 把 `Request` 序列化打到 `api/query.py`,后者调**同一个** `QueryService.query`——即上图从 QueryService 起完全一致。

## 3. 一次写(`insert`)怎么流

```
Seekbase.insert(table, rows)                          # client.py → Request(op=insert)
  → LocalExecutor.execute → WriteService.insert       # service/write.py:薄编排(~5 行,只管顺序+原子)
        StoreService.validate(...)                    #   store 拥有:列校验 + dup-pk(PK 约束兜底)
        SearchService.embed_records(...)              #   search 拥有:内联 embed + jieba 分词
        FileService.write_puts(...)                   #   file 拥有落盘形状;文件最先(canonical)
        StoreService.commit_rows(...)                 #   随行 INSERT(含 _vec/_tok)+ FTS 重建(一个 bridge 块)
        TicketRegistry.issue("insert")                #   → struct.Ticket
  → Ticket                                            # client.insert 取 .id;api 出口 .to_wire()
```

写是**同步**的:`insert` 返回即向量已落库、可搜;ticket 恒 `done`。删除同理,`WriteService.delete` 软删(`UPDATE deleted_ds`)。

## 4. 关键接缝

- **两形态接缝 = `Request` + executor**:`client` 只构造 `Request`,不认识本地/远程;`LocalExecutor`(`service/dispatch.py`)→service,`HttpExecutor`(`api/remote.py`)→HTTP。`Ticket` 在 HTTP 边界 `to_wire`/`from_wire`,所以 client 本地/远程拿到的都是 `Ticket`,传输无关。
- **单写者 bridge**(`runtime/bridge.py`):一个单线程持有唯一 DuckDB 连接,所有 DuckDB 操作串行化。用例服务决定粗粒度顺序(files 先于 db),领域服务内部保原子(`StoreService.commit_rows` = INSERT + FTS 一个块)。
- **canonical 是文件**:`StoreService` 的 DuckDB 是从文件可重建的派生层;`AdminService.rebuild` 重放镜像重灌(见 [store.md](store.md))。

## 5. 与其他文档

- [store.md](store.md):两层存储(files canonical / DuckDB 派生)、rebuild、一致性。
- [search.md](search.md):`search()` 重写 + vss+fts RRF + jieba。
- [time_machine.md](time_machine.md):`ds`/`deleted_ds` 可见性谓词。
- [schema.md](schema.md):`searchable` 如何接线 `_vec`/`_tok`。
- [../../DESIGN.md](../../DESIGN.md):完整工程设计(边界、依赖、分期)。
