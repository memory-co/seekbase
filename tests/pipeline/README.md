# pipeline — SPL 式管道机制

## 这个场景在测什么

query = `stage | stage`,每段按**首 token** 分类:命中 operator registry →
算子段;不命中 → **整段是一条 DuckDB SQL(SQL 是缺省)**。整条管道编译成
**一条 `WITH` SQL**(每段一个 CTE,`_in` = 上一段),交给 store 执行——
seekbase 不自建管道运行时(docs/works/pipeline-as-anything.md /
pipeline-runtime-optimize.md)。

1. **切分**:`|` 只在顶层切;`||`(SQL 拼接)和字符串字面量里的 `|` 不切;空段报错。
2. **SQL 缺省**:纯 SQL 零管道原样执行(`WITH …` 开头照走);首 token 不命中
   registry 的段是 SQL——**不存在「未知算子」**,错的话是 SQL 语法错。
3. **降级融合**:`search | grep | SELECT` 三段融成一条 `WITH` SQL;`grep` 翻成
   `WHERE regexp_matches(…)`;`scan` = 可见性视图;SQL 段也能打头。
4. **位置从签名推导**:source(`search`/`scan`)只能打头;中段(`grep`)不能打头。
5. **参数分配**:用户 `params` 按 SQL 段的 `?` 顺序分配(跳过字面量里的 `?`);
   多余参数报错。
6. **读写守卫穿过管道**:管道段里塞 `DELETE` 一样被拒。
7. **registry 守卫**:算子名不得撞 SQL 引导关键字;重名显式报错;没有任何
   可执行格(duck/bash 任一)的算子拒绝注册。
8. **bash runtime(切段 + JSONL 桥)**:`scan | sh | SELECT` 三 phase(duck→bash→duck),
   `_in` 以 JSONL 过 stdin/stdout、回程 `read_json_auto` 推类型;相邻 bash 段融成
   **一条进程链**;bash 段不能打头(有界 query 无 bash source);子进程失败/超时
   → `QueryError` 带 stderr。

## 不在这测什么

- `search` 段的检索质量 / 时间窗走 [`search/`](../search/)
- 策略判定细节走 [`policy/`](../policy/);流式走 [`streaming/`](../streaming/)

## fixture 来源

- `db`(`tests/conftest.py`)—— `cards`(`issue` 可搜)+ `FakeEmbedder`
- `split_pipeline` / `Registry` / `Operator` 直接单测(无 db)
