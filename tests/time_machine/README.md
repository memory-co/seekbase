# time_machine — ds 时间窗(`ds_start` / `ds_end`)

## 这个场景在测什么

每行带引擎代管的 `ds`(写入日 `YYYYMMDD`);`query` 的 `ds_start`/`ds_end` 按 `ds`
分区列裁剪,给出时光机 / 时间段语义(见 [works/time_machine.md](../../docs/works/time_machine.md)):

1. **时光机回退**:`ds_end` 早于写入日 → 看不到;`ds_end` 在未来 → 看得到。
2. **区间 / 下界**:`ds_start` 在未来 → 看不到;窗口不含今天 → 空。
3. **只读闸**:`query` 传非 `SELECT`/`WITH` 语句 → `ReadOnlyError`(写只能走 insert/delete)。
4. **格式校验**:`ds_start`/`ds_end` 非 `YYYYMMDD` → `QueryError`。

## 不在这测什么

- 墓碑本身(删完还在)走 [`insert_only/`](../insert_only/)
- `search` 段叠加时间窗走 [`search/`](../search/)
- HTTP 上的时间窗走 [`server/`](../server/)

## fixture 来源

- `db`(`tests/conftest.py`)—— 标准 `cards` 嵌入库,自动关
