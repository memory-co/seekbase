# quickstart — 最基础的本地用法(端到端)

## 这个场景在测什么

最朴素的一条路,像一段上手教程 —— **纯本地、不起 server、连 embedder 都不用**:

1. 在一个本地目录建库(`Seekbase.open`,schema 里没有 `searchable` 列 → 纯 DuckDB、零向量);
2. 写入几行(`insert`);
3. 查出来(`select` / `count`);
4. 删一行(`delete`,打墓碑);
5. 再查 —— 删掉的看不见了。

外加一条:关库再开同一目录,数据还在(**本地库 = 一个目录,可持久**)。

## 不在这测什么

- 语义 `search()` / embedder 走 [`schema/`](../schema/) 的接线 + M3
- 墓碑物理还在、raw SQL 能看到 的细节走 [`insert_only/`](../insert_only/)
- 过滤/排序/分页的完整算子走 [`basic_orm/`](../basic_orm/)
- server / HTTP 形态走 [`server/`](../server/)

## fixture 来源

- 无共享 fixture —— 直接 `Seekbase.open(tmp_path, schema=…)`,自带一个最小 schema
  (只 `id` + `text`,无 `searchable`),让这条路读起来就是「开箱最小用法」。
