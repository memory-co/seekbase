# works — 设计与工作文档

深入某个子系统的设计推演。总览设计见仓库根的 [DESIGN.md](../../DESIGN.md);这里放更细的专题。

> **方向说明**:本套文档已按 **pipeline 方向(设计稿,未落)**对齐——query 是一根 SPL 式管道(`stage | stage`),**SQL 是一等公民、也是缺省**,检索/`grep`/`sh` 等都是**注册工具**,检索**引擎可插拔**(LanceDB / DuckDB-vss)。现网代码仍是旧形态(`search()` UDF + 单引擎 DuckDB);各文档状态行标注了与现网的差异。存储/时光机/写路径的底层机制多数不变,只是被重新框进管道架构。

**架构主线(先读这两篇):**

| 文档 | 主题 |
|---|---|
| [pipeline-as-anything.md](pipeline-as-anything.md) | **设计稿**:query = SPL 式管道,`stage \| stage`,一切皆表(`_in` ABI);**SQL 一等公民**——一段首 token 命中工具才走工具、否则整段是 SQL;`\|` 只在跨引擎/跨进程的**接缝**出现(§2.1「接缝才切」),纯 SQL query 零管道;检索退成一个 source 段,同管道可串 `bash`/HTTP/`grep`;代价是失去全局优化 + tool 段的安全围栏 |
| [tool-registry.md](tool-registry.md) | **设计稿**:万物皆注册工具——`search` 只是一条最佳实践,`find`/`sed`/`grep`/`sh` 平级注册;注册契约(格式契约 `accepts`/`emits` / 签名 / caps / handler)+ **权限范围**(Claude/Codex 式:能力 capability × 策略 policy,默认 `read-only`、`EXEC` 默认关、放行也在沙箱里),给 pipeline §9 的安全洞一道正式围栏 |
| [tool-plugin.md](tool-plugin.md) | **设计稿(作者视角)**:一个 plugin = 一个**可插拔算子**,框架只定算子 ABI(`search`/`grep`/`jq`/SQL 段插的是同一个)。**三个正交轴**:① 格式契约 `accepts`/`emits`(不用 `kind`,source/sink 位置从格式推导)+ coercion;② 无状态 vs **服务型**(`start`/`run`/`stop`,`search` 的常驻引擎 + RAM 索引);③ **参考 Flink 的流动性/有界性**——一次性 `run` vs 推式 `process(chunk,ctx,out)`、`bounded` 传播、**SQL 段要求有界 ⇒ 把 `tail -f` 的挂死变成编译期错误**、Arrow reader 早停(只借 process + 有界性,watermark/checkpoint/keyed state 不借);外加 `ctx` capability 唯一入口、caps + 输出 schema、三个例子 + 测试 + 诚实代价 |

**子系统专题:**

| 文档 | 主题 |
|---|---|
| [architecture.md](architecture.md) | 代码分层与调用链:一切皆 service(领域服务 Store/Search(可插拔后端)/File + 用例服务 Pipeline/Write/Admin)+ api / struct / runtime;读 = `PipelineService` 管道编译(rewrite 层退休),两形态复用同一套 service |
| [store.md](store.md) | 两层存储(files canonical / 派生 = 结构化 DuckDB + **可插拔检索后端** duck-vss/LanceDB):角色分工、files 布局(按天分区 / 每表 `<表>.jsonl`)、insert 的「文件最先」原子性顺序,以及后续写入出问题时如何用 files 校准 |
| [search.md](search.md) | 检索 = 管道的一个 **source 段**,引擎**可插拔**(LanceDB / DuckDB-`vss`+`fts`):后端契约、hybrid RRF、jieba 中文分词、两后端的 fd/内存取舍、as-of 下推;`search()` UDF 退休 |
| [time_machine.md](time_machine.md) | 用 `ds` 分区实现时光机:两对日期字段、可见性谓词、`ds_start`/`ds_end` 语义、写一次(穿越 create/delete)、无物理删(历史永久);as-of 作为 source 段 `@asof` 入参下推进检索候选 |
| [schema.md](schema.md) | 声明式表结构:一处声明如何推导出 DDL / **可插拔检索后端**派生 / 文件镜像 / 元数据列;`searchable` 接检索后端、`search` 是 source 非函数;类型系统、校验规则、schema 演进 |
| [ticket.md](ticket.md) | 写回执 / 操作日志(设计):ticket 是同步写的回执(非异步句柄、非提交闸);为什么从内存 dict 换成独立、落盘、状态-only 的按天分区 JSONL 日志;自定位 id、保留清理、JSONL vs DuckDB 取舍 |
| [concurrency.md](concurrency.md) | async 执行 / 读写分离 / 写管道(设计):Bridge 为什么存在(async↔阻塞 DuckDB)、读为何排在写后、读走 cursor+MVCC 拆分(管道 transform 段走读 cursor)、写收敛成一条看得见生命周期的 worker(循环 + ticket)、a 同步 / b 异步、批处理 |
