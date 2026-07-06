# seekbase 测试 — 按场景组织

照 memory.talk 的路数:每个子目录是**一个场景**,有自己的 `README.md`(在测
什么 / 不在这测什么 / fixture 来源)和 `test.py`。相关的用例合并在一个场景下,
跟「按代码模块切文件」解耦。

## 场景一览

| 目录 | 测什么 |
|---|---|
| [`quickstart/`](quickstart/) | 最基础的本地用法(端到端):开库 → 写 → 查 → 删 → 再查,不起 server、不需 embedder;含关库重开数据仍在 |
| [`read_write/`](read_write/) | SQL `query` 读 + 异步 `insert`/`delete`(ticket)round-trip:批量、参数化、`count`、重复主键 latest-wins |
| [`file_mirror/`](file_mirror/) | canonical 文件镜像:写落 `ds=…/<表>.jsonl`、删是 append 墓碑、`rebuild` 从文件重灌恢复精确状态 |
| [`search/`](search/) | SQL 里的 `search()`:按相似度排序 + `_score`、和结构化过滤/时间窗组合、删后搜不到、无 searchable 表报错 |
| [`insert_only/`](insert_only/) | `delete()` 只打墓碑:正常查询看不到、重删匹配 0;端口无 update/upsert |
| [`time_machine/`](time_machine/) | `ds_start`/`ds_end` 时间窗:时光机回退、区间、只读闸、ds 格式校验 |
| [`schema/`](schema/) | SCHEMA 校验(list 形态:table/columns/primary/类型/decimal)+ 未知列被拒 + searchable 须 str + 需 embedder + 高级类型 DDL round-trip |
| [`server/`](server/) | server 形态:同一套调用走 HTTP、错误保型过线(含 `NotFound`)、鉴权、health、runner 外部注入 |
| [`embedder_live/`](embedder_live/) | **真实 embedding API** 端到端(需环境变量 `QWEN_KEY` + `SEEKBASE_EMBED_URL`,默认 skip)|

## 共享 fixture / helper(`conftest.py`)

- `db` —— 标准嵌入 `Seekbase` + 一个 `cards` schema(happy path 用)
- `pair` —— `(server_db, client)`:嵌入 server + 绑定它的 in-process HTTP client
- `open_db(data_root, *, schema=, embedder=)` —— 自定义 schema / embedder 的工厂
- `client_for(server_db, *, app_key=, client_key=)` —— 自定义鉴权的 client
- `serve_pair(data_root, ...)` —— `open_db` + 匹配鉴权的 client
- `FakeEmbedder` —— 确定性、零依赖,满足 searchable 列接线(向量到 M3 才真跑)

## 加新场景

1. 新目录 `tests/场景名/`,放 `__init__.py`
2. 写 `README.md`:**测什么 / 不测什么 / fixture 来源**
3. 写 `test.py`:测试本体
4. 不需要在任何地方登记 —— pytest 自动收集
