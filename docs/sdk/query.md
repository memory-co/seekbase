# 读:`db.query`

```python
rows = await db.query(
    sql,                  # str:SPL 管道;纯 SQL 就是一条 DuckDB SELECT
    *,
    params=None,          # list:位置参数,按段内 ? 顺序分配
    ds_start=None,        # "YYYYMMDD":时间窗下界(ds >= ds_start)
    ds_end=None,          # "YYYYMMDD":时间窗上界;只给它 = 时光机回到那天
    as_task=False,        # True → 后台跑,立即返回 task id(str)
) -> list[Row] | str      # 行(dict 列表);as_task=True 时是 task id
```

## query 是一根 SPL 管道,SQL 是缺省

`stage | stage | …`。每段看**首 token**:命中注册算子(`search`/`scan`/`grep`/`sh`/`jq`/自定义)→ 走算子;**不命中 → 整段就是一条 DuckDB SQL**。纯 SQL 查询零管道、原样执行——不存在「未知算子」这种错误。

```python
# 纯 SQL(零管道,和普通 DuckDB 无异)
await db.query("SELECT card_id, issue FROM cards WHERE kind = ? ORDER BY created_at DESC LIMIT 20",
               params=["issue"])

# 检索:search 源段产 _in(可见列 + _score),后面一整条 SQL 吃它
await db.query("search cards 'pty 终端' | SELECT card_id, _score FROM _in ORDER BY _score DESC LIMIT 10")

# 多段:算子 + SQL 融合成一条 WITH(优化器看穿全链)
await db.query("search cards 'tmux' | grep 'ERROR' --field issue | SELECT card_id FROM _in")

# bash 段(要 Policy(mode="sandboxed"),见 policy.md)
await db.query("scan cards | sh 'grep tmux' | SELECT count(*) AS c FROM _in")
```

**约定**:上一段的产物恒名 **`_in`**;整条管道编译成一条 DuckDB `WITH` SQL(bash 段按切段 + JSONL 桥执行),seekbase 不自建执行器。

## 内建算子速查

| 段 | 位置 | caps | 说明 |
|---|---|---|---|
| `search <表> '<文本>' [--col <列>] [--k <n>]` | source | PURE | hybrid 检索(向量 vss/lance + BM25,RRF 融合,jieba 分词),产可见列 + `_score`;表有多个 searchable 列时 `--col` 必填;`--k` 默认 100 |
| `scan <表>` | source | PURE | 表的可见行(时间窗生效),显式管道头 |
| `grep '<正则>' --field <列>` | 中段 | PURE | 按列正则过滤 `_in`(翻成 `WHERE regexp_matches`,零开销) |
| `sh '<命令>'` | 中段 | **EXEC** | 逃生舱:`_in` 以 JSONL 过 stdin/stdout;默认策略被拒 |
| `jq '<脚本>'` | 中段 | **EXEC** | `jq -c` 整形 JSONL;默认策略被拒 |
| `watch` / `ingest` | — | — | 流式专用,进 `query` 报错(见 [stream.md](stream.md)) |

规则:source 只能打头;中段不能打头;相邻 bash 段融成一条进程链;`\|\|`(SQL 拼接)和字符串字面量里的 `|` 不会被切分。

## `params`

位置参数按**段内 `?` 出现顺序**分配(跳过字面量里的 `?`);多余参数报 `QueryError`。算子段的参数(如 search 的查询向量)由框架内部插入,不占用户 params。

## 时间窗 / 时光机(`ds_start` / `ds_end`)

作用于**整条管道**:结构化可见性视图和 search 候选共用同一条 as-of 谓词——回到某天,搜到的也是那天的存活集。

```python
await db.query("search notes '缓存' --col body | SELECT id FROM _in", ds_end="20260103")
```

## `as_task`

```python
tid = await db.query("…很重的分析…", as_task=True)     # 立即返回 task id
st = await db.wait(tid)                                # 轮询到 done/failed
rows = await db.task_result(tid)                       # 从结果文件读回行
```

- 嵌入形态**默认不升级**(await 就是要答案);`as_task=True` 显式后台跑。
- HTTP 形态:server 侧 `wait_ms`(默认 5000ms)内跑完 → 直接回行(零 task 开销);超时 → 查询**继续跑**、就地收编成 task → 客户端拿到 task id 转轮询。请求体带 `"as_task": true` 则立即 202。
- 后台 task 有 max runtime(默认 300s,超时转 failed);见 [task.md](task.md)。

## 只读守卫

必须是单条 `SELECT`(`WITH…SELECT` 放行);管道段里塞 DML 一样被拒。非只读 → `ReadOnlyError`。

## 错误

| 情况 | 异常 |
|---|---|
| SQL 语法错 / 未知表列(含首 token 不是算子的「伪命令」) | `QueryError` |
| 非只读语句 | `ReadOnlyError` |
| source 不在头 / 中段打头 / 空段 / 参数多余 | `QueryError` |
| 算子被策略拒(如默认策略下的 `sh`) | `PermissionDenied` |
| `watch`(无界源)进有界 query / `ingest` 进 query | `QueryError` |
| bash 段子进程失败 / 超沙箱时长 | `QueryError`(带 stderr / 超时说明) |
