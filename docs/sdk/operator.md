# 自定义算子:继承 `Operator`

一个算子 = 一个 `Operator` 子类,经 `Seekbase.open(operators=[…])` 注册后,你的 verb 和内建 `search`/`grep` 平权地出现在管道里。完整契约与设计理由见 [works/operator-plugin.md](../works/operator-plugin.md);本页是速查。

## 最小算子

```python
from seekbase import Operator, Cap, QueryError
from types import SimpleNamespace

class Top(Operator):
    """top <n> —— 取 _in 的前 n 行(演示:纯 duck 原生降级)。"""
    name = "top"
    caps = frozenset({Cap.PURE})

    def parse_args(self, tokens):
        if len(tokens) != 1 or not tokens[0].isdigit():
            raise QueryError("usage: top <n>")
        return SimpleNamespace(n=int(tokens[0]))

    def optimize_duck(self, prev, args):                 # 中段:吃 prev
        return f"SELECT * FROM {prev} LIMIT {args.n}", []

db = await Seekbase.open("./data", schema=SCHEMA, operators=[Top])   # 类或实例都行
await db.query("scan cards | top 5 | SELECT card_id FROM _in")
```

## 覆写点速查

```python
class Operator:
    name: str                          # 必填;不得撞 SQL 引导关键字(select/with/from/…)
    caps: frozenset = {Cap.PURE}       # 诚实声明碰什么外界资源(policy 判定的唯一依据)
    bounded: bool = True               # bash source 才需要关心:流会不会结束

    def parse_args(self, tokens) -> args        # shell 风格 token → 参数;可按参数改 self.caps
    async def prepare(self, args, ctx) -> None  # 编译期派生(要服务的活,如 embed);可存 args._x

    # 执行格(四选一非空;有 optimize 走原生零开销,没有走物化桥)
    def optimize_duck(self, prev, args) -> (sql, params)   # duck 原生:一段 SQL(中段吃 prev)
    def optimize_duck(self, args) -> (sql, params)         # source 形态:不吃 prev(签名推导位置)
    def optimize_bash(self, args) -> list[str]             # bash 原生:一条 argv(stdin/stdout 是介质)
    # run_duck / run_bash:物化格(留位契约面)

    async def start(self, ctx) -> None          # 服务型:open 时一次(开引擎/载索引,存 self)
    async def stop(self) -> None                # close 时一次(拆常驻资源)
```

关键规则(都由框架**推导**,不用声明字段):

- **位置从签名推导**:`optimize_duck(self, args)`(不吃 prev)= source,只能打头;`(self, prev, args)` = 中段。bash-only 算子缺省中段(stdin 是它的上游),bash source(如 `watch`)覆写 `is_source()`。
- **`optimize_*` 无 ctx**:只产代码不碰数据;要服务的派生(embed / 查 schema)放 `prepare`(async,有 ctx:`ctx.store` / `ctx.embedding` / `ctx.schema` / `ctx.ds_start` / `ctx.ds_end`)。
- **多格必须语义等价**:同一条 query 换 runtime 不能换答案——多格算子请配 differential test。
- **caps 要诚实**:policy 只看声明;`parse_args` 里可按参数升降(`grep <pat>` PURE / `grep <pat> <path>` FS_READ)。
- **服务型**(要复用贵资源:引擎连接、RAM 索引)才覆写 `start`/`stop`;handle 存 `self`,`run`/`optimize` 里绝不重开。

## 注册

```python
db = await Seekbase.open("./data", schema=SCHEMA, operators=[
    Top,                        # 类:框架实例化
    MySearch(uri="…"),          # 实例:需要构造参数时自己配好
])
```

- 重名 / 撞 SQL 关键字 / 无任何执行格 → `QueryError`(open 时早失败)。
- 定义子类**不等于**注册——必须显式列进 `operators=`(隐式注册 = 导入即扩权限面,不做)。
- 自定义算子和内建同受 policy 约束:声明了 `Cap.EXEC` 的,默认 read-only 下照样被拒。
