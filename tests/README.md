# seekbase 测试 — 按场景组织

照 memory.talk 的路数:每个子目录是**一个场景**,有自己的 `README.md`(在测
什么 / 不在这测什么 / fixture 来源)和 `test.py`。相关的用例合并在一个场景下,
跟「按代码模块切文件」解耦。

## 场景一览

| 目录 | 测什么 |
|---|---|
| [`basic_orm/`](basic_orm/) | 核心 `insert` / `select` / `count` round-trip + 过滤/排序/分页 + 默认 select 带 `created_at` + 上下文管理器 |
| [`insert_only/`](insert_only/) | `delete()` 只打墓碑:正常查询看不到、但行物理还在(raw SQL 能看到 `deleted_at`);没有 update 路径 |
| [`time_machine/`](time_machine/) | `as_of` 连接只读 + 把世界回退到那个时刻(嵌入形态) |
| [`schema/`](schema/) | SCHEMA 校验(主键/保留列/类型/files 占位符)+ 未知列被拒 + searchable 列必须给 embedder + `search()` 已接受但 M3 才落 |
| [`server/`](server/) | server 形态:同一条链走 HTTP、错误保型过线、`as_of` 只读在 HTTP 上也生效、鉴权、health、runner 外部注入 |

## 共享 fixture / helper(`conftest.py`)

- `db` —— 标准嵌入 `Seekbase` + 一个 `cards` schema(happy path 用)
- `pair` —— `(server_db, client)`:嵌入 server + 绑定它的 in-process HTTP client
- `open_db(data_root, *, schema=, embedder=, as_of=)` —— 自定义 schema/embedder/as_of 的工厂
- `client_for(server_db, *, app_key=, client_key=, as_of=)` —— 自定义鉴权/回退的 client
- `serve_pair(data_root, ...)` —— `open_db` + 匹配鉴权的 client
- `FakeEmbedder` —— 确定性、零依赖,满足 searchable 列接线(向量到 M3 才真跑)

## 加新场景

1. 新目录 `tests/场景名/`,放 `__init__.py`
2. 写 `README.md`:**测什么 / 不测什么 / fixture 来源**
3. 写 `test.py`:测试本体
4. 不需要在任何地方登记 —— pytest 自动收集
