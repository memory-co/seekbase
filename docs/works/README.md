# works — 设计与工作文档

深入某个子系统的设计推演。总览设计见仓库根的 [DESIGN.md](../../DESIGN.md);这里放更细的专题。

| 文档 | 主题 |
|---|---|
| [store.md](store.md) | 两层存储(files canonical / DuckDB 派生:事件表 + vss+fts 检索派生表):角色分工、files 布局(按天分区 / 每表 `<表>.jsonl`)、insert 的「文件最先」原子性顺序,以及后续写入出问题时如何用 files 校准 |
| [search.md](search.md) | 语义 + 全文 hybrid 检索(单引擎 DuckDB `vss`+`fts`):派生表、HNSW 增量 vs FTS 重建、jieba 中文分词、RRF 融合,以及为什么把 LanceDB 收进 DuckDB(fd/EMFILE) |
| [time_machine.md](time_machine.md) | 用 `ds` + 事件重放实现时光机:两对日期字段、重放判定、`ds_start`/`ds_end` 语义、多版本完备性、无物理删(历史永久) |
| [schema.md](schema.md) | 声明式表结构:一处声明如何推导出 DDL / vss+fts 检索派生 / 文件镜像 / 元数据列;类型系统、声明式的理由、校验规则、schema 演进 |
