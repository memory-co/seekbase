# 权限:`Policy` × `Cap`

一段管道能不能跑某算子,看它声明的能力(`Cap`)是否落在当前策略(`Policy`)允许的范围内——**编译期判定**,拒了管道不启动(`PermissionDenied`)。设计见 [works/operator-registry.md §6](../works/operator-registry.md)。

```python
from seekbase import Policy, Cap

db = await Seekbase.open("./data", schema=SCHEMA, embedder=emb,
                         policy=Policy(mode="sandboxed"))
```

## `Policy`

```python
Policy(
    mode="read-only",         # "read-only" | "sandboxed" | "trusted"
    allow=(),                 # 算子名白名单;非空则只有名单内的能跑
    deny=(),                  # 算子名黑名单;压过一切(含 trusted)
    deny_caps=(),             # 能力级黑名单,如 (Cap.EXEC,) 一句话封掉所有 EXEC 算子
    exec_timeout=30.0,        # bash 段的墙钟超时(秒),超时整进程组 kill
)
```

**判定顺序:`deny` > `allow` > 模式缺省**。

## 三种模式

| 模式 | 允许的能力 | 语义 |
|---|---|---|
| `read-only`(默认) | `PURE` `FS_READ` `NET` | query 是数据接口,默认不能写盘/起进程——`sh`/`jq` 被拒 |
| `sandboxed` | 上 + `EXEC` `FS_WRITE` | 放行 shell,但子进程在沙箱边界里:scratch cwd、最小 env、独立进程组、墙钟超时→整组 kill |
| `trusted` | 全部 | 本机可信调用方,全放(`deny`/`deny_caps` 仍生效) |

**诚实边界**:进程内不强制网络隔离——策略层(deny EXEC / 白名单)是第一道墙;`ask`(交互确认)态设计有、实现延后。

## `Cap` — 算子声明的能力

| Cap | 含义 | 内建算子 |
|---|---|---|
| `Cap.PURE` | 只碰 `_in`,不碰外界 | `search` `scan` `grep` `ingest` |
| `Cap.FS_READ` | 读文件系统 | `watch` |
| `Cap.FS_WRITE` | 写文件系统 | — |
| `Cap.NET` | 联网 | —(embedder 的网络由框架中介,不经算子 caps) |
| `Cap.EXEC` | 起任意子进程 | `sh` `jq` |

## 例子

```python
# 默认:sh 被拒
db = await Seekbase.open(..., )                                  # Policy() = read-only
await db.query("scan cards | sh 'cat'")                          # PermissionDenied

# 升级放行(进沙箱)
db = await Seekbase.open(..., policy=Policy(mode="sandboxed"))
await db.query("scan cards | sh 'grep tmux' | SELECT count(*) AS c FROM _in")   # OK

# 黑名单压过 trusted
Policy(mode="trusted", deny=("sh",)).check(...)                  # sh 永拒

# 能力级封禁:所有 EXEC 类一句话关掉(抗「新算子漏配」)
Policy(mode="trusted", deny_caps=(Cap.EXEC,))

# 白名单收紧:只许检索类
Policy(allow=("search", "scan", "grep"))
```
