# insert_only — 只增、删即打墓碑

## 这个场景在测什么

焊死的不变性:`delete()` 的唯一语义是**打墓碑**(写 `deleted_at`),不是物理删。

1. **删完正常查询看不到**:`count()` / `select()` 自动滤掉墓碑行。
2. **行物理还在**:`db.sql()` 直查能看到那一行,且 `deleted_at` 有值 —— 证明
   历史没被抹掉(时光机严谨性的地基,见 [`time_machine/`](../time_machine/))。
3. **没有 update 路径**:端口上不存在 `update` / `upsert` 方法。

## 不在这测什么

- `as_of` 从墓碑重建历史视图走 [`time_machine/`](../time_machine/)
- `vacuum` 物理清墓碑 —— M4,尚未实现
- HTTP 上的 delete 走 [`server/`](../server/)

## fixture 来源

- `db`(`tests/conftest.py`)—— 标准 `cards` 嵌入库,自动关
