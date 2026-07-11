# works — 设计与工作文档

深入某个子系统的设计推演。总览设计见仓库根的 [DESIGN.md](../../DESIGN.md);这里放更细的专题。

| 文档 | 主题 |
|---|---|
| [architecture.md](architecture.md) | 代码分层与调用链:一切皆 service(领域服务 Store/Search/File + 用例服务 Query/Write/Admin)+ api / struct / runtime,一次读/写怎么流过它们,两形态如何复用同一套 service |
| [store.md](store.md) | 两层存储(files canonical / DuckDB 派生:每业务表一张物理表,vss+fts 就地长在表上):角色分工、files 布局(按天分区 / 每表 `<表>.jsonl`)、insert 的「文件最先」原子性顺序,以及后续写入出问题时如何用 files 校准 |
| [search.md](search.md) | 语义 + 全文 hybrid 检索(单引擎 DuckDB `vss`+`fts`):检索列长在业务表上、向量随行写定 vs FTS 同步重建、jieba 中文分词、RRF 融合,以及为什么把 LanceDB 收进 DuckDB(fd/EMFILE) |
| [time_machine.md](time_machine.md) | 用 `ds` 分区实现时光机:两对日期字段、可见性谓词、`ds_start`/`ds_end` 语义、写一次(穿越 create/delete)、无物理删(历史永久) |
| [schema.md](schema.md) | 声明式表结构:一处声明如何推导出 DDL / vss+fts 检索派生 / 文件镜像 / 元数据列;类型系统、声明式的理由、校验规则、schema 演进 |
| [ticket.md](ticket.md) | 写回执 / 操作日志(设计):ticket 是同步写的回执(非异步句柄、非提交闸);为什么从内存 dict 换成独立、落盘、状态-only 的按天分区 JSONL 日志;自定位 id、保留清理、JSONL vs DuckDB 取舍 |
| [concurrency.md](concurrency.md) | async 执行 / 读写分离 / 写管道(设计):Bridge 为什么存在(async↔阻塞 DuckDB)、读为何排在写后、读走 cursor+MVCC 拆分、写收敛成一条看得见生命周期的 WritePipeline(worker 循环 + ticket)、a 同步 / b 异步、批处理 |
