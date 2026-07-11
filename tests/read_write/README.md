# read_write — SQL 读 + 同步写 round-trip

## 这个场景在测什么

嵌入形态最普通的使用姿势 —— `insert` 写几行(同步、返 ticket)、`query` 传 SQL
取回、`delete` 打墓碑,语义正确:

1. **写是同步的**:`insert` 返 ticket(已 `done`);写完 `query` 立即读得到。
2. **读是 SQL**:`query("SELECT … WHERE … ORDER BY … LIMIT …")`,聚合 / 过滤 / 排序都在 SQL 里。
3. **批量 insert**、参数化 `?`、`count(*)`、重复主键报错(写一次)都对。

## 不在这测什么

- 墓碑语义细节走 [`insert_only/`](../insert_only/)
- `ds_start`/`ds_end` 时间窗走 [`time_machine/`](../time_machine/)
- schema 校验 / 未知列走 [`schema/`](../schema/)
- HTTP / server 形态走 [`server/`](../server/)

## fixture 来源

- `db`(`tests/conftest.py`)—— 标准 `cards` 嵌入库,自动关
