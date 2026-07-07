# readonly_guard — `query` 只读、写类 SQL 一律被拒

## 这个场景在测什么

`query` 是**只读逃生舱**——写只能走 `insert` / `delete`。这里把各种「想通过 SQL
写/改/删」的路都试一遍,确认**统一报 `ReadOnlyError`、且数据毫发无损**:

1. **直白的写/DDL/副作用**:`INSERT` / `UPDATE` / `DELETE` / `DROP` / `CREATE` /
   `ALTER` / `ATTACH` / `SET` / `CALL` / `COPY … TO` → `ReadOnlyError`(DuckDB 把
   它们判成非 `SELECT` 语句)。
2. **绕过尝试**:`WITH … DELETE` / `WITH … INSERT` / `WITH … UPDATE`——首 token
   是 `WITH` 但整条是 DML,**naive「首词是不是 SELECT」会被绕过**;这里用 DuckDB
   自己的 statement-type 判定拦住(回归测试:曾真能删掉物理表)。
3. **多语句**:`SELECT 1; DROP TABLE …` → `ReadOnlyError`(必须恰一条读语句)。
4. **合法读仍放行**:`WITH … SELECT` / 子查询 / 聚合 照常работает。
5. **delete 的 where 也挡 `;`**:`where` 里塞第二条语句 → `QueryError`。
6. **HTTP 上同样**:错误保型过线(`ReadOnlyError` 还原成原类)。

## 不在这测什么

- 时间窗 / 事件重放语义走 [`time_machine/`](../time_machine/);读写 round-trip 走 [`read_write/`](../read_write/)。
- **只读的信息泄露面**(纯 `SELECT` 能 `read_json('/etc/…')` 读文件系统、`PRAGMA database_list` 读元数据)—— 那是 **server 沙箱**议题,不属于「写保护」;本场景只保证**写不进去**。DuckDB 把这类只读 PRAGMA 判成 `SELECT`,故放行。

## fixture 来源

- `db` / `pair`(`tests/conftest.py`)—— 标准 `cards`(物理表 `_sb_cards`)
