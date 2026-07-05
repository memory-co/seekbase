# time_machine — as-of 回退 + 只读闸(嵌入形态)

## 这个场景在测什么

`Seekbase.open(..., as_of=T)` 把整个连接变成一台时光机:

1. **世界回退到 T**:只看得见 T 及之前建、且 T 时刻还没删的行。取一个远早于
   任何写入的 T,`count()` 应为 0;取一行自己的 `created_at` 作 T,那行可见。
2. **只读闸**:时光机连接上写(`insert` / `delete`)一律 `ReadOnlyError` —— 往
   过去写没有意义,由引擎强制、不靠调用方自觉。

## 不在这测什么

- 墓碑本身(删完还在)走 [`insert_only/`](../insert_only/)
- as-of 谓词改写在 HTTP 上也生效走 [`server/`](../server/)
- 原始 SQL 的 as-of 视图注册 —— M4,M1 的 `sql()` 暂不回退
- `vacuum` 丢历史 —— M4,尚未实现

## fixture 来源

- `open_db(data_root, *, as_of=)`(`tests/conftest.py`)—— 同一目录先写当前态、
  再以某个 `as_of` 重开只读连接
