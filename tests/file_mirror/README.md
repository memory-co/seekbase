# file_mirror — canonical 文件镜像 + rebuild

## 这个场景在测什么

每次写自动落成按天分区的 `files/ds=YYYYMMDD/<表>.jsonl`(append-only),文件是
canonical、DuckDB 是可从文件重建的派生层:

1. **写落文件**:`insert` 往 `ds=今天/<表>.jsonl` append 完整行;文件可 grep、
   拿一行就是那条数据的完整快照(含 `ds`/`created_at`)。
2. **删是 append 墓碑**:`delete` 往同一 jsonl append 一条 `{"_deleted": pk, …}`
   墓碑,**不回改已写的行**。
3. **rebuild 保真**:清掉 `duck.db` 重开(派生层空)→ `rebuild()` 按 `ds` 顺序
   replay jsonl → 库恢复到删删改改后的准确状态。
4. **软删的行 replay 后仍是软删**(rebuild 保真)。

## 不在这测什么

- 时间窗查询走 [`time_machine/`](../time_machine/)
- 检索那一路(vss+fts)走 [`search/`](../search/)

## fixture 来源

- `open_db`(`tests/conftest.py`)—— 用一个无 `searchable` 的最小 schema 直接开库,
  好断言文件内容
