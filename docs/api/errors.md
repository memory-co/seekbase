# 错误

一套异常层级,两形态通用;HTTP 形态下**错误保型过线**——server 侧抛什么类型,客户端侧还原成同类型。

## 层级(`from seekbase import ...`)

```
SeekbaseError                 所有 seekbase 失败的基类
├── SeekbaseUnavailable       底层开不了 / 不可服务 → 宿主应回 503
├── SchemaError               open 时 SCHEMA 校验失败
├── EmbedderInvalid           缺 embedder,或维度/契约不符
├── ReadOnlyError             往时光机(as_of)连接写,或 sql() 传了非只读语句
├── QueryError                查询错:未知表/列、不支持的算子
└── NotSupportedYet           已设计但当前里程碑未实现(如 M1 的 search())
```

## 错误 ↔ HTTP 状态码

`POST /v1/execute` 出错时返回 `{"error": {"type": <类名>, "message": <文本>}}`,状态码按类型映射:

| 异常 | HTTP 状态 |
|---|---|
| `NotSupportedYet` | `501` |
| `SeekbaseUnavailable` | `503` |
| `SchemaError` / `EmbedderInvalid` / `ReadOnlyError` / `QueryError` / 其它 `SeekbaseError` | `400` |
| 鉴权失败 | `401`(`type: "Unauthorized"`) |
| 未预期的内部异常 | `500`(`type: "Internal"`) |

客户端 `HttpExecutor` 收到非 200 时,按 `error.type` 在异常注册表里查回对应类并抛出;认不出的类型退化成 `SeekbaseError`。

## 两种形态

```python
from seekbase import ReadOnlyError

# 函数形态:直接抛
try:
    await db.table("cards").insert(row)   # as_of 连接
except ReadOnlyError:
    ...

# HTTP 形态:同样的 except 生效——server 侧的 ReadOnlyError 过线后
# 在客户端重新抛成 ReadOnlyError(而不是某个 HTTP 错误)
```

这条「错误保型」是两形态**调用代码逐字节相同**的一部分:异常处理也不用改。
