# API 参考

> 函数调用形态(Python SDK)见 [../sdk/](../sdk/);本目录是 HTTP 报文级契约。

seekbase 的本地 API,所有接口收发 JSON:

- **读**:`POST /v1/query` 传一根 **SPL 管道**(纯 SQL 零管道原样执行;`search`/`grep` 等是注册算子段;时间窗 `ds_start`/`ds_end` 同在)。`wait_ms`(默认 5000)内跑完 → 200 直接回行;超时 → 查询继续跑、就地升级成 task → `202 {task, state}` 转轮询。
- **写**:insert / delete **同步**——响应返回时已落库(files → 行 → 索引),带回一个**出生即 done 的 task**;rebuild 是**真后台 task**(立即回 pending,轮询到 done)。
- **task**:统一操作句柄(写回执 / rebuild / 慢查询),`GET/POST /v1/tasks…` 查询与取消。

拿句柄、起 server、声明 schema、注入 embedder、策略与自定义算子都在 [setup.md](setup.md)。

```
Query     POST  /v1/query               读:SPL 管道(SQL 缺省 + 算子段 + ds 时间窗);wait_ms 超时 → 202 task → query.md
Insert    POST  /v1/insert              写(同步):提交行,返 task(已 done)                     → insert.md
Delete    POST  /v1/delete              写(同步):按条件软删墓碑,返 task(已 done)             → delete.md
Tasks     GET   /v1/tasks               最近的 task 列表                                          → tasks.md
          GET   /v1/tasks/{id}          单个 task 状态                                            → tasks.md
          GET   /v1/tasks/{id}/result   后台查询的结果行                                          → tasks.md
          POST  /v1/tasks/{id}/cancel   取消一个后台 task                                         → tasks.md
Writes    GET   /v1/writes/{ticket}     /v1/tasks/{id} 的兼容别名                                 → tasks.md
Rebuild   POST  /v1/rebuild             从文件重建派生层(后台 task,返 pending)                 → admin.md
Health    GET   /v1/health              健康:{"ready": bool}                                     → admin.md
```

## 鉴权

server 配了 `api_key` 时每个请求须带 `Authorization: Bearer <api_key>`;不匹配 → `401`,`{"error": {"type": "Unauthorized", "message": "bad api key"}}`。单个可选 bearer token,多租户 auth 非目标。

## 错误

统一响应体 `{"error": {"type": <类名>, "message": <文本>}}`,状态码按类型:

| 状态 | 异常类型 | 含义 |
|---|---|---|
| 400 | `QueryError` | 未知表/列、SQL 语法错、管道形状错(source 不在头、空段、参数多余、无界源进有界 query)、bash 段失败/超时、`ds_*` 格式错 |
| 400 | `ReadOnlyError` | `query` 传了非只读语句(按语句类型判定,挡 `WITH…DML`/多语句,穿透管道段)|
| 400 | `SchemaError` / `EmbedderInvalid` | schema 校验 / embedder 契约失败 |
| 401 | `Unauthorized` | bearer token 不匹配 |
| 403 | `PermissionDenied` | 算子能力超出策略(如默认 `read-only` 下用 `sh`);编译期拒,管道不启动 |
| 404 | `NotFound` | task id 不存在 |
| 503 | `SeekbaseUnavailable` | 底层开不了 / 不可服务 |
| 500 | `Internal` | 未预期内部异常 |

**错误保型过线**:客户端(`connect`)收到非 2xx 时按 `error.type` 重建同类型异常并抛出——server 侧的 `PermissionDenied` 在客户端还是 `PermissionDenied`。层级:`SeekbaseError`(基类)→ `SeekbaseUnavailable` / `SchemaError` / `EmbedderInvalid` / `ReadOnlyError` / `QueryError` / `NotFound` / `PermissionDenied`。

## 设计要点

- **query 是一根 SPL 管道,SQL 是缺省**:一段首 token 命中注册算子(`search`/`scan`/`grep`/`sh`/`jq`/自定义)才走算子,否则整段就是一条 DuckDB SQL——纯 SQL 查询零管道、原样执行。整条管道编译成一条 `WITH` SQL(bash 段切段桥接),不为搜索单开接口。
- **写同步、读己之写**:向量在 insert 时就地 embed 随行写入,响应返回即可被 `query`/`search` 读到;写的 task 出生即 done。
- **task 是统一句柄**:写回执 = 出生即 done 的 task;rebuild / 慢查询 = 真 pending→done 后台 task;结果落文件按保留期 GC([../works/task.md](../works/task.md))。
- **只增、引擎强制**:没有 update/upsert;`delete` 唯一语义是打 `deleted_ds` 墓碑(非物理删),`query` 默认自动滤掉墓碑行。**没有物理删 / vacuum,历史永久保留**。
- **时间窗 per-request**:`ds_start`/`ds_end` 是 `query` 的参数(只给 `ds_end` = 时光机),作用于整条管道(search 候选共用同一 as-of 谓词)。
- **权限 per-server**:算子按能力(`PURE`/`FS_READ`/`NET`/`EXEC`…)受 server 端 `Policy` 约束,默认 `read-only`(`sh`/`jq` 被拒);见 [setup.md](setup.md)。
- **两形态同一套语义**:函数形态(`Seekbase.open` 进程内 / `Seekbase.connect` 客户端)构造的就是这些请求,调用代码逐字节相同。流式摄取(`db.stream`)是嵌入专属,无 HTTP 面。

## 文档清单

| 文档 | 覆盖 |
|---|---|
| [query.md](query.md) | 读:SPL 管道 + 算子段 + 时间窗 + `wait_ms`/`as_task` 升级 |
| [insert.md](insert.md) | 同步写:提交 + task 回执 |
| [delete.md](delete.md) | 同步删:软删墓碑 |
| [tasks.md](tasks.md) | task:列表 / 状态 / 结果 / 取消 |
| [admin.md](admin.md) | `rebuild`(后台 task)/ `health` |
| [setup.md](setup.md) | `open` / `connect` / `serve` + schema + embedder + policy + 自定义算子 |
