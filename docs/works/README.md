# works — 设计与工作文档

深入某个子系统的设计推演。总览设计见仓库根的 [DESIGN.md](../../DESIGN.md);这里放更细的专题。

> **方向说明**:pipeline 方向**已开始落地**——M1(管道编译器 + `Operator` 基类 + `search`/`scan`/`grep`,duck runtime,`seekbase/operator/` + `service/pipeline_service.py`)与**可插拔检索后端**(vss / lance,`Seekbase.open(search_backend=…)`)已上线,`search()` UDF 退休。query 是一根 SPL 式管道(`stage | stage`),**SQL 是一等公民、也是缺省**。**未落**:bash runtime + 能力×策略沙箱、runtime 指派/融合优化、流式摄取;各文档状态行标注各自的落地程度。

**架构主线(先读这两篇):**

| 文档 | 主题 |
|---|---|
| [pipeline-as-anything.md](pipeline-as-anything.md) | **设计稿**:query = SPL 式管道,`stage \| stage`,一切皆表(`_in` ABI);**SQL 一等公民**——一段首 token 命中算子才走算子、否则整段是 SQL;`\|` 只在跨引擎/跨进程的**接缝**出现(§2.1「接缝才切」),纯 SQL query 零管道;检索退成一个 source 段,同管道可串 `bash`/HTTP/`grep`;代价是 external 段的安全围栏(「失去全局优化」那笔账已被后端方案消掉,见下)|
| [operator-registry.md](operator-registry.md) | **设计稿**:万物皆注册算子——`search` 只是一条最佳实践,`find`/`sed`/`grep`/`sh` 平级注册;注册契约(`Operator` 子类:参数签名 / caps / 执行方法,**无 `accepts`/`emits`**——格式是 runtime 介质)+ **权限范围**(Claude/Codex 式:能力 capability × 策略 policy,默认 `read-only`、`EXEC` 默认关、放行也在沙箱里),给 pipeline §9 的安全洞一道正式围栏 |
| [pipeline-runtime-optimize.md](pipeline-runtime-optimize.md) | **设计稿(后端)**:**管道不自己跑**——整条被降级到一个 **pipeline runtime**;runtime 是**开放集**(今 DuckDB `WITH` / bash pipeline 两个;物化 `run_*` 是两者的兜底、不是第三个 runtime;可扩展,§10 给加新 runtime 的四点契约)。**关键分层(§1.1):runtime ⊥ 算子内部的引擎后端**——DuckDB/bash 是承载整条管道的 runtime,LanceDB/duck-vss 是 `search` 一个算子背后的后端,两层正交、别混。代价阶梯(**原生 `optimize_*` 0 成本 > 同 runtime 物化 `run_*` > 切段一次物化 / 内联桥每批 marshal**)、runtime 指派 = 一条最短路(对 runtime 数量无感)、连续同 runtime 段融合、`grep` 写 `optimize_duck`(翻成 `WHERE`)如何把切换点消成 0;外加多格的语义等价风险(differential test 是准入条件)|
| [operator-plugin.md](operator-plugin.md) | **设计稿(作者视角)**:一个算子 = 一个 **`Operator` 子类**——声明放类属性、行为放方法覆写,层次只有一层(`ExternalCommand` 预设默认值)。**为什么是继承不是记录**:服务型要 `self` 存 handle、几格降级路共享私有方法、一父两子复用(`Search` → `LanceSearch`/`VssSearch`)、`parse_args` 按参数变 caps。**核心:两轴四方法**——`{原生 optimize_* × 物化 run_*} × {duck × bash}`,四格全可选、≥1 非空,`optimize` 是 0 成本原生降级、`run` 是有屏障的物化兜底(`run_bash` 自己 `ctx.spawn` 起子进程)。**无 `accepts`/`emits`**:格式是 runtime 介质、position 从签名推导,一切从「覆写了什么」推。另有覆写 `start`/`stop` = 服务型;有界性(`tail -f` 进 duck = 编译期报错,流式细节见 pipeline-streaming);`ctx` capability 唯一入口、四个例子(`Grep`/`Search`/`Jq`/`Rerank`)+ 测试 + 诚实代价 |
| [pipeline-streaming.md](pipeline-streaming.md) | **设计稿(流式)**:**把 bash 管道当简易流框架**——不造流引擎,借内核管道白送的无界流 + 背压 + 重启。核心推导:**能做 streaming 的 source ⟺ 无界 ⟺ 没有 `optimize_duck` ⟺ 只能 bash 启动 + 常驻**。无界流「落」进 DuckDB 靠 **sink 命令式微批写入**(不是关系流入 `WITH`——那会挂死);sink 白嫖写路径(WriteService + files-first + 批处理摊 FTS)。**stream 写 / query 读干净分离**:流只摄取,开窗/聚合交给 landed 表上的有界 SQL。例子:监听 Claude Code 的 `*.jsonl` → `jq` 抽字段 → `ingest` 落 DuckDB → 可搜。诚实代价:at-least-once + 幂等 sink、无水位线/窗口、embed on ingest 贵、每流一个常驻进程——**不是 Flink** |

**子系统专题:**

| 文档 | 主题 |
|---|---|
| [architecture.md](architecture.md) | 代码分层与调用链:一切皆 service(领域服务 Store/Search(可插拔后端)/Embedding/File + 用例服务 Pipeline/Write/Admin)+ api / struct / runtime;读 = `PipelineService` 管道编译(rewrite 层退休),两形态复用同一套 service |
| [store.md](store.md) | 两层存储(files canonical / 派生 = 结构化 DuckDB + **可插拔检索后端** duck-vss/LanceDB):角色分工、files 布局(按天分区 / 每表 `<表>.jsonl`)、insert 的「文件最先」原子性顺序,以及后续写入出问题时如何用 files 校准 |
| [search.md](search.md) | 检索 = 管道的一个 **source 段**,引擎**可插拔**(LanceDB / DuckDB-`vss`+`fts`):后端契约、hybrid RRF、jieba 中文分词、两后端的 fd/内存取舍、as-of 下推;`search()` UDF 退休 |
| [time_machine.md](time_machine.md) | 用 `ds` 分区实现时光机:两对日期字段、可见性谓词、`ds_start`/`ds_end` 语义、写一次(穿越 create/delete)、无物理删(历史永久);as-of 作为 source 段 `@asof` 入参下推进检索候选 |
| [schema.md](schema.md) | 声明式表结构:一处声明如何推导出 DDL / **可插拔检索后端**派生 / 文件镜像 / 元数据列;`searchable` 接检索后端、`search` 是 source 非函数;类型系统、校验规则、schema 演进 |
| [ticket.md](ticket.md) | 写回执 / 操作日志(设计):ticket 是同步写的回执(非异步句柄、非提交闸);为什么从内存 dict 换成独立、落盘、状态-only 的按天分区 JSONL 日志;自定位 id、保留清理、JSONL vs DuckDB 取舍 |
| [concurrency.md](concurrency.md) | async 执行 / 读写分离 / 写管道(设计):Bridge 为什么存在(async↔阻塞 DuckDB)、读为何排在写后、读走 cursor+MVCC 拆分(管道 transform 段走读 cursor)、写收敛成一条看得见生命周期的 worker(循环 + ticket)、a 同步 / b 异步、批处理 |
