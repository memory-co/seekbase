# vacuum — 显式丢历史(按行清死行)

## 这个场景在测什么

`vacuum(before=D)` 物理清掉 `deleted_ds < D` 的**死行**——DuckDB 行 + 文件里那
些行的全部事件(insert + 墓碑)+ 向量。**不是**整块删分区:活行、以及删于
`≥ D` 的行都保留。

1. **只清「D 之前删掉」的行**:删于今天的行,`before` 取过去 → 清 0;取未来 → 清掉。
2. **活行不动**:vacuum 后正常行照在。
3. **历史真没了**:vacuum 后 `rebuild` 从文件重灌,被清的行不再复活(它的 insert/
   墓碑事件都从 jsonl 里删了)。
4. **`before` 格式**:非 `YYYYMMDD` → `QueryError`。

## 不在这测什么

- 时光机可见性谓词走 [`time_machine/`](../time_machine/)
- 文件镜像 / rebuild 基础走 [`file_mirror/`](../file_mirror/)

## fixture 来源

- `db`(`tests/conftest.py`)—— 标准 `cards`
- `open_db`(无 searchable 的 `notes`)—— rebuild-after-vacuum 用,免向量干扰
