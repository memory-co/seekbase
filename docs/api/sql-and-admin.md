# SQL 直查与管理动作

## `sql` — 只读逃生舱

```python
rows = await db.sql("SELECT kind, count(*) AS n FROM cards GROUP BY kind")
```

- **只读**:语句必须以 `SELECT` / `WITH` 开头,否则 `ReadOnlyError`——写只能走 ORM(保住 insert-only 与三写不变性)。
- 用于 join / 聚合 / 窗口 / 对账等 ORM 链表达不了的分析。
- `[M4]` `as_of` 连接下,`sql()` 尚未自动回退(需 as-of 视图注册);当前直查看到的是当前态。

HTTP 形态:

```json
POST /v1/execute
{"op": "sql", "statement": "SELECT kind, count(*) AS n FROM cards GROUP BY kind", "as_of": null}
→ 200 {"result": [{"kind": "issue", "n": 3}]}
```

非 `SELECT/WITH` 语句 → `400` + `ReadOnlyError`。

## `flush` — 读己之写

```python
await db.flush()   # 排干 outbox,让刚写入的行对 search() 立即可见
```

- 结构化查询(不带 `search()`)永远强一致,不需 flush;`flush()` 是给「写完立刻要语义搜到」的场合把最终一致收敛成强一致。
- `[M3]` 当前为 no-op(向量引擎/outbox 尚未落地);接口先在,契约与 HTTP 语义从第一天稳定。

HTTP:`{"op": "flush"}` → `{"result": null}`。

## `rebuild` — 从文件重建派生层 `[M2]`

```python
await db.rebuild()   # 通读 files 声明的全部文件 → 重灌 DuckDB + LanceDB
```

- 「表丢了能从文件重建」这条不变性的内建动作(见 [works/store.md](../works/store.md))。
- 当前抛 `NotSupportedYet`(随文件镜像 M2 落地)。

HTTP:`{"op": "rebuild"}`(M1 → `501` + `NotSupportedYet`)。

## `vacuum` — 显式丢历史 `[M4]`

```python
await db.vacuum(before="2026-06-01T00:00:00Z")   # 物理清 T 之前的墓碑(行+向量+文件)
```

- 墓碑常驻 = 空间换历史;`vacuum` 是唯一会真正物理删的动作,**明说这是在丢历史**。
- 时光机连接(`as_of`)下不可调用。
- 当前抛 `NotSupportedYet`(随时光机 M4 落地)。

HTTP:`{"op": "vacuum", "before": "2026-06-01T00:00:00Z"}`(M1 → `501`)。

| 动作 | 一致性/副作用 | M1 状态 |
|---|---|---|
| `sql` | 只读,强一致(as_of 未回退) | ✅ |
| `flush` | 收敛最终一致 → 强一致 | no-op |
| `rebuild` | 从文件重灌双引擎 | `NotSupportedYet` |
| `vacuum` | 物理删墓碑(丢历史) | `NotSupportedYet` |
