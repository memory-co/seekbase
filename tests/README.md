# seekbase 测试 — 按场景组织

照 memory.talk 的路数:每个子目录是**一个场景**,有自己的 `README.md`(在测
什么 / 不在这测什么 / fixture 来源)和 `test.py`。相关的用例合并在一个场景下,
跟「按代码模块切文件」解耦。

## 场景一览

> 组织上有意混了几把尺子(功能子系统 / 端到端旅程 / 横切契约 / 传输形态),
> `journey/` 是把典型正路径连起来的**大巡游**,其余目录做各自的边界与负路径细测。

| 目录 | 测什么 |
|---|---|
| [`journey/`](journey/) | 一条端到端主线(本地、无 HTTP):建库 → 混合当前/历史 ds 批量写 30 条中文数据 → 结构化查询 → 中文 `search` 管道段 → 时光机回溯 → 软删可见性 → 重开持久化 → `rebuild` → 写一次性,典型功能连成一个故事走一遍 |
| [`quickstart/`](quickstart/) | 最基础的本地用法(端到端):开库 → 写 → 查 → 删 → 再查,不起 server、不需 embedder;含关库重开数据仍在 |
| [`read_write/`](read_write/) | SQL `query` 读 + 同步 `insert`/`delete`(task 回执)round-trip:批量、参数化、`count`、重复主键报错 |
| [`pipeline/`](pipeline/) | SPL 管道机制:切分(`\|\|`/字面量)、SQL 缺省、算子降级融合、位置推导、参数分配、只读守卫穿管道、registry 守卫、bash runtime 切段桥 |
| [`policy/`](policy/) | 能力×策略:deny > allow > 模式缺省、默认拒 `sh`、sandboxed 放行、denylist 压过 trusted、`PermissionDenied` 过线保型 |
| [`tasks/`](tasks/) | 统一操作句柄:写出生即 done、rebuild 后台 task、`as_task` 查询+结果文件、取消、HTTP `wait_ms` 202 升级、runaway 不挂关库 |
| [`streaming/`](streaming/) | 常驻流 `watch \| … \| ingest`:落库+pk 幂等去重、checkpoint 重启、半行等待、无界源进 query 编译期拒、jq 中段整形 |
| [`file_mirror/`](file_mirror/) | canonical 文件镜像:写落 `ds=…/<表>.jsonl`、删是 append 墓碑、`rebuild` 从文件重灌恢复精确状态 |
| [`search/`](search/) | 管道 `search` 源段(vss / lance **双后端参数化**):按相似度排序 + `_score`、和结构化过滤/时间窗组合、删后搜不到、rebuild 后可搜、无 searchable 表报错 |
| [`insert_only/`](insert_only/) | `delete()` 只打墓碑:正常查询看不到、重删匹配 0;端口无 update/upsert |
| [`time_machine/`](time_machine/) | `ds_start`/`ds_end` 时间窗:时光机回退、区间、只读闸、ds 格式校验 |
| [`schema/`](schema/) | SCHEMA 校验(list 形态:table/columns/primary/类型/decimal)+ 未知列被拒 + searchable 须 str + 需 embedder + 高级类型 DDL round-trip |
| [`server/`](server/) | server 形态:同一套调用走 HTTP、错误保型过线(含 `NotFound`)、鉴权、health、runner 外部注入 |
| [`readonly_guard/`](readonly_guard/) | `query` 只读:写/DDL/`WITH…DML`/多语句一律 `ReadOnlyError`(按 DuckDB 语句类型),数据毫发无损 |
| [`embedder_live/`](embedder_live/) | **真实 embedding API** 端到端(需环境变量 `QWEN_KEY` + `SEEKBASE_EMBED_URL`,默认 skip)|

## 共享 fixture / helper(`conftest.py`)

- `db` —— 标准嵌入 `Seekbase` + 一个 `cards` schema(happy path 用)
- `pair` —— `(server_db, client)`:嵌入 server + 绑定它的 in-process HTTP client
- `open_db(data_root, *, schema=, embedder=, search_backend=)` —— 自定义 schema / embedder / 检索后端的工厂
- `client_for(server_db, *, app_key=, client_key=)` —— 自定义鉴权的 client
- `serve_pair(data_root, ...)` —— `open_db` + 匹配鉴权的 client
- `FakeEmbedder` —— 确定性、零依赖(bag-of-chars,有排序信号),满足 searchable 列接线

## 加新场景

1. 新目录 `tests/场景名/`,放 `__init__.py`
2. 写 `README.md`:**测什么 / 不测什么 / fixture 来源**
3. 写 `test.py`:测试本体
4. 不需要在任何地方登记 —— pytest 自动收集
