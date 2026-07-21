# policy — 能力 × 策略授权

## 这个场景在测什么

一段管道能不能跑某算子,看它声明的 `caps` 是否落在当前 `Policy` 允许的范围内
(operator-registry.md §6),**编译期判定**——deny 命中则管道根本不启动。
决策顺序:**deny > allow > 模式缺省**。

1. **模式缺省**:`read-only`(默认)拒 `EXEC`/`FS_WRITE`;`sandboxed` 放行
   `EXEC`(进程跑在沙箱边界里:scratch cwd、最小 env、进程组、墙钟超时);
   `trusted` 全放。
2. **denylist 压过一切**:`trusted` + `deny=("sh",)` 仍拒 sh。
3. **能力级 deny**:`deny_caps=(Cap.EXEC,)` 一句话封掉所有 EXEC 算子。
4. **allowlist 收紧**:给了白名单就只有名单内的能跑(PURE 也不例外)。
5. **集成**:默认策略下 `sh` 在 query 里被 `PermissionDenied` 编译期拒;
   `sandboxed` 下同一条管道正常出结果。

## 不在这测什么

- 沙箱逃逸/网络隔离(进程内不强制网络隔离,策略层是第一道墙——诚实边界)
- `ask` 交互确认态(设计有、实现延后)

## fixture 来源

- `db`(conftest,默认 read-only)+ 显式 `Policy(mode=...)` 的 `Seekbase.open`
