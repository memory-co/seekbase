# basic_orm — 核心结构化读写 round-trip

## 这个场景在测什么

`Seekbase` 嵌入形态最普通的使用姿势 —— `insert` 写几行,链式 `select` /
`count` 能按条件取回,过滤 / 排序 / 分页语义正确。同时锁住两条约定:

1. **默认 `select`(不点列)带上 `created_at`**:元数据列是引擎代管的,但默认
   投影要能看见写入时间,不用手工声明。
2. **`in_` / `like` 等算子编译成参数化查询**:值走绑定、列名走白名单,顺带证明
   链是惰性、`await` 才执行。

## 不在这测什么

- 墓碑 `delete()` / insert-only 走 [`insert_only/`](../insert_only/)
- `as_of` 时光机走 [`time_machine/`](../time_machine/)
- schema 校验 / 未知列 / `search()` 延后走 [`schema/`](../schema/)
- HTTP / server 形态走 [`server/`](../server/)

## fixture 来源

- `db`(`tests/conftest.py`)—— 标准 `cards` 嵌入库,自动关
