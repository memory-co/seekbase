# SDK 参考(Python 函数调用)

`seekbase` 的 Python 面:**一个端口类 `Seekbase`**,两种拿法(嵌入 `open` / 远程 `connect`),之后**调用代码逐字节相同**。HTTP 报文级契约见 [../api/](../api/);本目录按函数讲——每个方法的签名、参数、返回、错误。

```python
from seekbase import Seekbase

db = await Seekbase.open("./data", schema=SCHEMA, embedder=emb)   # 或 Seekbase.connect(url)
rows = await db.query("search cards 'pty 终端' | SELECT card_id, _score FROM _in LIMIT 10")
await db.wait(await db.insert("cards", {"card_id": "c1", "issue": "…"}))
await db.close()
```

## 目录

| 页 | 覆盖的调用 |
|---|---|
| [open.md](open.md) | `Seekbase.open` / `Seekbase.connect` / `close` / `ready` / `async with` / `services` |
| [query.md](query.md) | `db.query` —— SPL 管道(SQL 缺省 + `search`/`scan`/`grep`/`sh`/`jq` 算子段)、`params`、时间窗、`as_task` |
| [write.md](write.md) | `db.insert` / `db.delete` / `db.wait` / `db.write_status` |
| [task.md](task.md) | `db.tasks` / `db.task_status` / `db.task_result` / `db.cancel_task` / `db.rebuild`、`Task` 字段 |
| [stream.md](stream.md) | `db.stream` / `StreamHandle`(`stop` / `running` / `exception`) |
| [policy.md](policy.md) | `Policy`(mode / allow / deny / deny_caps)、`Cap`、判定顺序 |
| [operator.md](operator.md) | 自定义算子:继承 `Operator`、`parse_args` / `prepare` / `optimize_duck` / `optimize_bash` / `start` / `stop`、注册 |
| [embedder.md](embedder.md) | `Embedder` 协议、内置 `ApiEmbedder` |
| [errors.md](errors.md) | 异常层级:`SeekbaseError` 及其子类,HTTP 状态码映射 |

## 公开导出

```python
from seekbase import (
    Seekbase,                     # 端口
    Policy, Cap, Operator,        # 策略 / 能力 / 自定义算子基类
    Task, Ticket,                 # 操作句柄(Ticket 是 Task 的旧名别名)
    Row, Hit, Request,            # 数据形状
    Embedder,                     # embedder 注入协议
    SeekbaseError, SeekbaseUnavailable, SchemaError, EmbedderInvalid,
    ReadOnlyError, QueryError, NotFound, PermissionDenied,
)
```

设计背景(为什么长这样)在 [../works/](../works/):管道模型 [pipeline-as-anything](../works/pipeline-as-anything.md)、算子契约 [operator-plugin](../works/operator-plugin.md)、权限 [operator-registry](../works/operator-registry.md)、task [task](../works/task.md)、流式 [pipeline-streaming](../works/pipeline-streaming.md)。
