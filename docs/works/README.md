# works — 设计与工作文档

深入某个子系统的设计推演。总览设计见仓库根的 [DESIGN.md](../../DESIGN.md);这里放更细的专题。

| 文档 | 主题 |
|---|---|
| [store.md](store.md) | 存储层三写形态(files / DuckDB / LanceDB):角色分工、files 目录结构规划、insert 的「文件最先」原子性顺序,以及后续写入出问题时如何用 files 校准 |
