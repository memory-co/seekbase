# works — 设计与工作文档

深入某个子系统的设计推演。总览设计见仓库根的 [DESIGN.md](../../DESIGN.md);这里放更细的专题。

| 文档 | 主题 |
|---|---|
| [store.md](store.md) | 存储层三写形态(files / DuckDB / LanceDB):角色分工、files 布局(按天分区 / 每表 `<表>.jsonl`)、insert 的「文件最先」原子性顺序,以及后续写入出问题时如何用 files 校准 |
| [time_machine.md](time_machine.md) | 用 `ds` + 事件重放实现时光机:两对日期字段、重放判定、`ds_start`/`ds_end` 语义、多版本完备性、无物理删(历史永久) |
| [schema.md](schema.md) | 声明式表结构:一处声明如何推导出 DDL / 双引擎 / 文件镜像 / 元数据列;类型系统、声明式的理由、校验规则、schema 演进 |
