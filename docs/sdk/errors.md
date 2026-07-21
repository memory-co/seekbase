# 错误:异常层级

全部异常继承 `SeekbaseError`;HTTP 形态**错误过线保型**——server 侧抛什么,client 侧还原成同类型异常。

```python
from seekbase import (
    SeekbaseError,            # 基类:一网打尽
    SeekbaseUnavailable,      #   底层库打不开/服务不可用 → 503
    SchemaError,              #   schema 声明非法 / 未知表 → 400
    EmbedderInvalid,          #   embedder 缺失或契约不符 → 400
    ReadOnlyError,            #   query 传了非只读语句 → 400
    QueryError,               #   查询/写入形态错(语法、未知列、主键重复、管道形状、bash 段失败…)→ 400
    NotFound,                 #   未知 task id 等寻址失败 → 404
    PermissionDenied,         #   算子能力超出策略(编译期拒,管道不启动)→ 403
)
```

## 常见触发速查

| 异常 | 典型场景 |
|---|---|
| `SchemaError` | `open` 时 schema 非法;`insert`/`search` 指向未知表 |
| `EmbedderInvalid` | 有 `searchable` 列却没给 embedder;维度不符 |
| `ReadOnlyError` | `query("DELETE …")`;`WITH … DELETE`;多语句 |
| `QueryError` | SQL 语法/未知列;主键重复(写一次);管道:source 不在头、空段、参数多余、无界源进有界 query、bash 段失败/超时、`task_result` 时机不对 |
| `NotFound` | `task_status`/`wait` 传了未知 id |
| `PermissionDenied` | 默认策略下用 `sh`/`jq`;命中 `deny`/`deny_caps`;不在 `allow` 白名单 |

## 捕获建议

```python
from seekbase import QueryError, PermissionDenied, SeekbaseError

try:
    rows = await db.query(user_pipeline)
except PermissionDenied:
    ...   # 提示升级 policy,或去掉 EXEC 段
except QueryError as e:
    ...   # 用户查询错:把 str(e) 展示给用户即可(带 DuckDB 原始信息)
except SeekbaseError:
    ...   # 其余框架错:记日志、降级
```
