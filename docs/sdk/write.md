# 写:`db.insert` / `db.delete` / `db.wait`

写是**同步语义**(模式 a):调用返回时 files 镜像、结构化行、检索索引全部落定,**写完即可搜**(read-your-write)。返回的是 task id——写的 task **出生即 done**(原 ticket,[works/task.md §2](../works/task.md))。

## `db.insert`

```python
task_id = await db.insert(
    table,                # str:表名
    rows,                 # dict | list[dict]:一行或一批
) -> str                  # task id(tk_<ds>_<hex>)
```

- **主键写一次**:批内重复或与既有行撞 pk → `QueryError`(整批拒,不落任何行)。seekbase 只增,没有 update/upsert。
- 未知列 → `QueryError`;缺的列填 `NULL`。
- `searchable` 列在写入时同步 embed + jieba 分词落索引(这是写延迟的主要成分)。
- 引擎自动盖元数据列:`ds`(写入日)/ `created_at`;调用方永远不碰。

```python
tid = await db.insert("cards", [
    {"card_id": "c1", "issue": "pty tmux terminal", "kind": "issue", "n": 1},
    {"card_id": "c2", "issue": "redis cache", "kind": "design", "n": 2},
])
st = await db.wait(tid)          # 立即返回(写是同步的,task 已 done)
assert st.state == "done"
```

## `db.delete`

```python
task_id = await db.delete(
    table,                # str
    *,
    where,                # str:必填的 WHERE 子句(不带 WHERE 字样)
    params=None,          # list:where 里 ? 的位置参数
) -> str
```

- **软删**:只盖 `deleted_ds`/`deleted_at` 墓碑,永不物理删——历史永久,时光机可回溯到删除前。
- 命中数在 task 上:`(await db.wait(tid)).matched`。
- 删除后立刻搜不到(检索候选带 `deleted_ds IS NULL` 谓词;索引项还在,靠谓词裁掉)。

```python
tid = await db.delete("cards", where="card_id = ?", params=["c1"])
print((await db.wait(tid)).matched)      # 1
```

## `db.wait` / `db.write_status`

```python
task = await db.wait(task_id, *, poll=0.05)   # 轮询到非 pending/running,返回 Task
task = await db.write_status(task_id)         # = db.task_status 的旧名别名
```

写的 task 出生即 done,所以 `wait` 对写**立即返回**;它真正的用途是等 rebuild / `as_task` 查询这类后台 task(见 [task.md](task.md))。

## 错误

| 情况 | 异常 |
|---|---|
| 未知表 | `SchemaError` |
| 未知列 / 批内或既有主键重复 | `QueryError` |
| `delete` 没给 `where` / where 非单条子句 | `QueryError` |
| 未知 task id | `NotFound` |
