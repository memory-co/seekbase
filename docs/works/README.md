# works — 设计与工作文档

深入某个子系统的设计推演。总览设计见仓库根的 [DESIGN.md](../../DESIGN.md);这里放更细的专题。

| 文档 | 主题 |
|---|---|
| [store.md](store.md) | 存储层三写形态(files / DuckDB / LanceDB):角色分工、files 目录结构规划、insert 的「文件最先」原子性顺序,以及后续写入出问题时如何用 files 校准 |
| [time_machine.md](time_machine.md) | 用 `ds` 日期分区实现时光机:创建/删除两对日期字段、可见性谓词、`ds_start`/`ds_end` 完整语义、完备性真值表、`vacuum` 正确语义 |
