# Query API

读接口:**传一根 SPL 管道,拿回行**。结构化查询、语义检索、时光机都在这一个接口里——

- **SQL 是缺省**:纯 SQL 就是普通 `SELECT`(join / 聚合 / 窗口都行),零管道、原样执行。
- **管道**:`stage | stage`,一段首 token 命中注册算子(`search`/`scan`/`grep`/`sh`/`jq`/自定义)才走算子,否则整段就是一条 DuckDB SQL;上一段的产物恒名 **`_in`**。整条编译成一条 `WITH` SQL(bash 段切段桥接),优化器看穿全链。
- **时间窗 / 时光机**:`ds_start` / `ds_end` 按日期分区圈定时间窗——只给 `ds_end` = 回到那天;作用于整条管道(search 候选共用同一 as-of 谓词)。
- **慢查询升级**:`wait_ms`(默认 5000)内跑完 → `200` 直接回行(零 task 开销);超时 → 查询**继续跑**、就地升级成 task → `202 {task, state}`,转 [tasks.md](tasks.md) 轮询取结果。`as_task: true` 跳过等待立即 202。

只读:必须是**单条只读语句**(穿透管道段)——写走 [insert.md](insert.md) / [delete.md](delete.md)。schema / embedder / 策略见 [setup.md](setup.md)。

**函数形态**:`await db.query("search cards 'pty 终端' | SELECT card_id, _score FROM _in ORDER BY _score DESC LIMIT 10")`

---

## POST /v1/query

### 请求体

```json
{
  "sql": "search cards 'pty 终端' | SELECT card_id, _score FROM _in WHERE kind = ? ORDER BY _score DESC LIMIT 10",
  "params": ["issue"],
  "ds_start": null,
  "ds_end": null,
  "wait_ms": 5000,
  "as_task": false
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `sql` | 是 | SPL 管道;纯 SQL = **单条只读 `SELECT`**(`WITH…SELECT` 放行,`WITH…DELETE`、多语句一律拒 → `ReadOnlyError`) |
| `params` | 否 | 位置参数,按**段内 `?` 出现顺序**分配(跳过字面量里的 `?`,防注入);多余参数 → `QueryError` |
| `ds_start` | 否 | 日期 `YYYYMMDD`,闭区间下界;只读 `ds >= ds_start` 的分区 |
| `ds_end` | 否 | 日期 `YYYYMMDD`,闭区间上界。只给它 = 时光机(见下) |
| `wait_ms` | 否 | 有界等待毫秒数,默认 5000;超时升级成 task(202) |
| `as_task` | 否 | `true` → 不等,立即提交后台 task 并 202 |

### 响应:200(在 `wait_ms` 内跑完)

```json
{"rows": [{"card_id": "c1", "_score": 0.03}]}
```

### 响应:202(超时升级 / `as_task`)

```json
{"task": "tk_20260722_ab12cd34ef56", "op": "query", "state": "running",
 "query": "search cards '…' | SELECT …", "submitted_at": "…"}
```

查询在 server 上继续跑;`GET /v1/tasks/{id}` 轮询状态、`GET /v1/tasks/{id}/result` 取行(见 [tasks.md](tasks.md))。后台 task 有 max runtime(默认 300s,超时转 `failed`)。

---

## 管道段速查

| 段 | 位置 | 说明 |
|---|---|---|
| `search <表> '<文本>' [--col <列>] [--k <n>]` | source(打头) | hybrid 检索:自动 embed + jieba 分词,向量(vss 或 lance 后端)+ BM25 用 **RRF** 融合;产该表可见列 + **`_score`**。表有多个 `searchable` 列时 `--col` 必填;`--k` 默认 100 |
| `scan <表>` | source | 表的可见行(时间窗生效)|
| `grep '<正则>' --field <列>` | 中段 | 按列正则过滤 `_in`(翻成 `WHERE regexp_matches`,零开销)|
| `sh '<命令>'` | 中段 | `_in` 以 JSONL 过 stdin/stdout;**EXEC** → 默认策略被拒(403),要 server 端 `Policy(mode="sandboxed")` |
| `jq '<脚本>'` | 中段 | `jq -c` 整形;同 `sh` 受策略约束 |
| 其余任何段 | — | **就是一条 DuckDB SQL**(over `_in`)——不存在「未知算子」 |

```json
// 检索 + 结构化:接缝后一整条 SQL 干完
{"sql": "search cards '为什么 pty 会让人想到 tmux' | SELECT card_id, issue, _score FROM _in WHERE kind = 'issue' ORDER BY _score DESC LIMIT 10"}

// 三段融合(仍编译成一条 WITH SQL)
{"sql": "search cards 'tmux' | grep 'ERROR' --field issue | SELECT card_id FROM _in"}

// bash 段(server 策略需 sandboxed)
{"sql": "scan cards | sh 'grep tmux' | SELECT count(*) AS c FROM _in"}
```

- `_score` 是 RRF 融合分(越大越相关),只在 `search` 段下游存在;旧的 `search()` SQL 函数已**退休**。
- `||`(SQL 拼接)和字符串字面量里的 `|` 不会被切分;source 只能打头、中段不能打头。
- **调用方永远不见向量、不算 embedding、不管分词**——只写文本。检索设计见 [`../works/search.md`](../works/search.md);引擎后端(vss / lance)是 server 端 `open` 的配置,对 API 透明。
- **读己之写**:写是同步的(insert 响应返回即已落库),写完立刻可搜。

---

## 时间窗 `ds_start` / `ds_end`(日期分区)

每行带引擎代管的分区列 `ds`(写入日,`YYYYMMDD`)。两参数是**闭区间**两端:

| 传入 | 语义 | 创建过滤(`ds`) |
|---|---|---|
| 都不传 | 全部(当前态) | 无 |
| 只 `ds_end` | **时光机**:回到那天及之前 | `ds <= ds_end` |
| 只 `ds_start` | 那天及之后、至今仍活 | `ds >= ds_start` |
| 都传 | 一个时间段 | `ds_start <= ds <= ds_end` |

```json
{"sql": "SELECT * FROM cards WHERE kind = 'issue'", "ds_end": "20260601"}
{"sql": "search notes '缓存' --col body | SELECT id FROM _in", "ds_end": "20260103"}
```

> 存活判定叠加**删除 horizon**:as-of `ds_end` 把「删于 `ds_end` 之后」的行仍算可见——`ds <= ds_end AND (deleted_ds IS NULL OR deleted_ds > ds_end)`,精确语义见 [`../works/time_machine.md`](../works/time_machine.md)。`search` 候选共用同一谓词:**回溯到某天,搜到的也是那天的存活集**(软删行留在索引里,靠谓词裁掉)。

- 分区裁剪,扫描量随时间窗收敛;粒度到**天**,日内用 `created_at` 二级过滤。
- per-request:一个 server 同时服务各自时间窗的多个请求。

---

## 错误

| 情况 | 状态 / type |
|---|---|
| 非只读语句(含 `WITH…DML`、多语句;管道段里塞 DML 同拒)| 400 `ReadOnlyError` |
| 未知表/列、SQL 语法错(含首 token 不是算子的「伪命令」)| 400 `QueryError` |
| 管道形状错:source 不在头 / 中段打头 / 空段 / 参数多余 | 400 `QueryError` |
| `search` 用在无 `searchable` 列的表 / 多可搜列没给 `--col` | 400 `QueryError` |
| `watch`(无界源)/ `ingest`(流 sink)进 query | 400 `QueryError` |
| bash 段子进程失败 / 超沙箱时长 | 400 `QueryError` |
| 算子能力超出策略(默认 read-only 下的 `sh`/`jq`)| 403 `PermissionDenied` |
| `ds_start` / `ds_end` 非 `YYYYMMDD` | 400 `QueryError` |
