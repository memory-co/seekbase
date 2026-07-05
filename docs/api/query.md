# 查询链(ORM)

`db.table(name)` 返回一个**惰性、不可变**的 `QueryBuilder`:每个算子返回新 builder,`await` 才执行。写(`insert`/`delete`)同样返回 awaitable;`count()` 是个返回 `int` 的终结算子。

## 概览

| 算子 | 作用 | `await` 返回 |
|---|---|---|
| `select(*cols)` | 投影;省略=声明列 + `created_at` | `list[dict]`(Row) |
| `insert(row \| rows)` | 追加(只增) | `None` |
| `delete()` | 打墓碑(非物理删) | `int`(墓碑行数) |
| `search(text)` | 语义检索算子 `[M3]` | `list[dict]`(带 `_score`) |
| `count()` | 计数(终结) | `int` |
| 过滤 | `eq neq gt gte lt lte in_ like ilike is_` | (链式) |
| 排序/分页 | `order(col, desc=) limit(n) offset(n)` | (链式) |

## 函数形态

```python
# 读
rows = await (db.table("cards")
    .select("card_id", "issue")
    .eq("kind", "issue").gte("created_at", "2026-06-01")
    .order("created_at", desc=True).limit(20).offset(0))

n = await db.table("cards").in_("card_id", ["c1", "c2"]).count()

# 写(只增)
await db.table("cards").insert({"card_id": "c1", "issue": "pty tmux", "kind": "issue"})
await db.table("cards").insert([{...}, {...}])           # 批量

# 删(打墓碑,返回受影响行数)
tombstoned = await db.table("cards").delete().eq("card_id", "c1")

# 语义检索(同一条链混结构化过滤)[M3]
hits = await (db.table("cards")
    .search("为什么 pty 会让用户想到 tmux")
    .eq("kind", "issue").limit(10))     # 每条 hit 是 dict + "_score"
```

要点:

- **只增、引擎强制**:没有 `update`/`upsert`;`delete()` 唯一语义是写 `deleted_at` 墓碑。正常查询自动滤掉墓碑行。
- **默认投影带 `created_at`**:`select()` 不点列时,返回声明列 + 引擎代管的 `created_at`。
- **列名走白名单**:未知列 → `QueryError`(顺带挡注入);值走参数绑定。
- `in_([])` 匹配空集(`count()==0`)。
- `is_(col, None)` → `IS NULL`。

## HTTP 形态

同样的链,经 `POST /v1/execute`,body 是序列化后的一个操作。`op` ∈ `select|insert|delete|count|search`。

**select** —— `db.table("cards").select("card_id","issue").eq("kind","issue").limit(20)`:

```json
POST /v1/execute
{
  "op": "select",
  "table": "cards",
  "columns": ["card_id", "issue"],
  "predicates": [{"op": "eq", "column": "kind", "value": "issue"}],
  "orders": [],
  "limit": 20, "offset": null,
  "rows": [], "statement": null, "before": null,
  "as_of": null
}
→ 200 {"result": [{"card_id": "c1", "issue": "pty tmux"}]}
```

**insert** —— `op:"insert"`,`rows:[{...}]`,`result` 为 `null`。
**delete** —— `op:"delete"`,带 `predicates`,`result` 为墓碑行数(int)。
**count** —— `op:"count"`,带 `predicates`,`result` 为 int。
**search** —— `op:"search"`(M1 返回 `501` + `NotSupportedYet`)。

谓词线格式:`{"op": "<eq|neq|gt|gte|lt|lte|in_|like|ilike|is_>", "column": "...", "value": ...}`(`in_` 的 `value` 是数组;`is_` 的 `value` 为 `null` 表示 `IS NULL`)。排序:`[[列, desc:bool], ...]`。

> 完整线格式(字段全集、响应/错误包)见 [server.md](server.md)。
