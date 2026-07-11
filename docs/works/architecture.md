# architecture — 分层与调用链

> seekbase 的代码分层与一次调用怎么流过它们。单引擎(DuckDB: 结构化 + `vss` + `fts`)+ 文件镜像;两种使用形态(嵌入 / HTTP)共用同一套 service。

## 1. 五层

```
client.py                两形态门面:Seekbase.open(嵌入)/ connect(远程);query/insert/delete/wait
  │
  ├── api/               HTTP 端点:一类接口一个文件(query/insert/delete/writes/rebuild/health)
  │      直接调 →
  └── _engine/executor   两形态接缝:LocalExecutor(op→service 薄转发)/ HttpExecutor(→HTTP)
         │
         ▼
      service/           用例编排(业务无关):query / write / admin(+ tickets 注册表)
         │
         ▼
      _engine/           机制层:duck(DuckDB 原语)· search(vss+fts)· files(镜像)· bridge · rewrite · text · clock

      struct/            贯穿各层的数据对象:Request · Ticket · Schema/TableSpec/Column · Row/Hit
```

- **`struct/`** 是数据,**`service/`** 是编排,**`_engine/`** 是机制,**`api/` + `client.py`** 是入口。数据只在 `struct/` 定义一次。
- **service 是唯一用例入口**:HTTP 走 `api/*.py` **直连** `db.services.*`;嵌入/远程走 `client → executor`,`LocalExecutor` 只把 `Request` 的 op 转发到同一批 service。两条路复用同一份编排,零重复。

## 2. 一次读(`search()` 查询)怎么流

```
Seekbase.query(sql)                                   # client.py:构造 Request(op=query)
  → LocalExecutor.execute                             # _engine/executor.py:op→service
    → QueryService.query                              # service/query.py:编排
        rewrite.extract_searches(sql)                 #   search(col,'x') → 占位 + specs
        rewrite.search_target(...)                    #   定表
        SearchEngine.hybrid(表,列,文本)              #   _engine/search.py:vss+fts RRF → [(pk,score)]
        DuckdbEngine.run_query(重写SQL, searches)     #   _engine/duck.py:只读守卫 + 可见性视图 + join
  → {"rows": [...]}                                   # 逐层原样返回
```

HTTP 形态:`connect` 的 `HttpExecutor` 把 `Request` 序列化打到 `api/query.py`,后者调**同一个** `QueryService.query`——即上图从 QueryService 起完全一致。

## 3. 一次写(`insert`)怎么流

```
Seekbase.insert(table, rows)                          # client.py → Request(op=insert)
  → LocalExecutor.execute → WriteService.insert       # service/write.py:编排三系统
        校验列 + dup-pk(existing_keys;PK 约束兜底)
        SearchEngine.embed_records(...)               #   内联 embed + jieba 分词
        FileMirror.append(...)                        #   文件最先(canonical)
        DuckdbEngine.commit_rows(...)                 #   随行 INSERT(含 _vec/_tok)+ FTS 重建(一个 bridge 块)
        TicketRegistry.issue("insert")                #   → struct.Ticket
  → Ticket                                            # client.insert 取 .id;api 出口 .to_wire()
```

写是**同步**的:`insert` 返回即向量已落库、可搜;ticket 恒 `done`。删除同理,`WriteService.delete` 软删(`UPDATE deleted_ds`)。

## 4. 关键接缝

- **两形态接缝 = `Request` + executor**(`_engine/executor.py`):`client` 只构造 `Request`,不认识本地/远程;`LocalExecutor`→service,`HttpExecutor`→HTTP。`Ticket` 在 HTTP 边界 `to_wire`/`from_wire`,所以 client 本地/远程拿到的都是 `Ticket`,传输无关。
- **单写者 bridge**(`_engine/bridge.py`):一个单线程 executor 持有唯一 DuckDB 连接,所有 DuckDB 操作串行化。service 决定粗粒度顺序(files 先于 db),引擎内部保原子(`commit_rows` = INSERT + FTS 一个块)。
- **canonical 是文件**:`_engine/duck.py` 是从文件可重建的派生层;`AdminService.rebuild` 重放镜像重灌(见 [store.md](store.md))。

## 5. 与其他文档

- [store.md](store.md):两层存储(files canonical / DuckDB 派生)、rebuild、一致性。
- [search.md](search.md):`search()` 重写 + vss+fts RRF + jieba。
- [time_machine.md](time_machine.md):`ds`/`deleted_ds` 可见性谓词。
- [schema.md](schema.md):`searchable` 如何接线 `_vec`/`_tok`。
- [../../DESIGN.md](../../DESIGN.md):完整工程设计(边界、依赖、分期)。
