# insert_only — 只增、删即打墓碑

## 这个场景在测什么

焊死的不变性:`delete()` 的唯一语义是**打墓碑**(写 `deleted_ds`),**永不物理删、也没有 vacuum**。

1. **删完正常查询看不到**:`query` 的 `count(*)` 归零(可见性视图自动滤掉墓碑)。
2. **重删匹配 0**:已经是墓碑的行不再被 `delete` 命中——证明它是**标记**、不是「真删又消失」。
3. **没有 update 路径**:端口上不存在 `update` / `upsert`。

## 不在这测什么

- `ds` 时间窗 / 时光机回退走 [`time_machine/`](../time_machine/)
- HTTP 上的 delete 走 [`server/`](../server/)

## fixture 来源

- `db`(`tests/conftest.py`)—— 标准 `cards` 嵌入库,自动关
