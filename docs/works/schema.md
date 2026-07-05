# schema — 声明式表结构(设计)

> 状态:设计。schema 是 seekbase 的**单一声明源**:声明一次,DDL / 双引擎同步 / 文件镜像 / 日期分区 / 时光机字段全部从它推导。对外声明用法见 [api/setup.md](../api/setup.md#3-声明-schema);本文讲设计与推导。

## 1. 定位:一处声明,处处推导

schema 描述每张表的**列、哪些列可语义搜、要不要落文件镜像**。它是 seekbase 唯一需要人手写的结构定义;从这一处,自动推导出:

```
                         SCHEMA(唯一声明源)
        ┌───────────────┬───────────────┬───────────────┐
   DuckDB DDL        LanceDB          files 布局      元数据列
   (结构化引擎)      collection       (canonical)     ds/created_at/
   建表/列/主键      (若有 searchable) (若声明 files)  deleted_ds/deleted_at
                                                      → 时光机
```

上层(repository / service)从此**不手写 SQL、不手写文件 ops、不手写向量接线**——加一列 = 改一处 schema,DDL / 迁移 / 三写同步全由 seekbase 兑现。

## 2. 声明格式

```python
SCHEMA = {
    "cards": {
        "columns": {"card_id": "str", "issue": "str", "kind": "str"},
        "primary": "card_id",                    # 主键列名(单独字段)
        "searchable": ["issue"],                 # 可选:哪些列可 search()
        "files": "cards/{card_id}.json",         # 可选:文件镜像
    },
}
```

四个键:`columns`(必)、`primary`(必)、`searchable`(可选)、`files`(可选)。

## 3. `columns`(列 → 类型)

`columns` 是纯 `列名: 类型` 映射,**不掺修饰**——主键单独走 `primary` 字段(§3.2)。

| 声明类型 | DuckDB 类型 | 文件镜像里怎么存 |
|---|---|---|
| `str` | `VARCHAR` | JSON 字符串 |
| `int` | `BIGINT` | JSON 数字 |
| `float` | `DOUBLE` | JSON 数字 |
| `bool` | `BOOLEAN` | JSON `true` / `false` |
| `decimal(p,s)` | `DECIMAL(p,s)` | **字符串**(避免 JSON 浮点丢精度) |
| `timestamptz` | `TIMESTAMP WITH TIME ZONE` | ISO-8601 带时区字符串 |
| `json` | `JSON` | 原生嵌套 JSON(可 grep) |

### 3.1 三个非标量 / 精确类型

前四种是标量;另三种按一条底线挑出来——**能否干净塞进 pretty-JSON + Arrow(LanceDB)+ DuckDB 三写往返、且可 grep**。所以 `blob` 等二进制**不收**(base64 进 JSON 又丑又不可 grep,违背 canonical 是文本的底线)。

- **`decimal(p,s)` —— 精确定点**。给精度 `p`、标度 `s`(如 `decimal(18,2)`)。用于金额、带样本量的分数等 `float`(有损二进制浮点)表达不了的值。**文件里存成字符串**(`"123.45"`)——JSON 只有浮点数,存字符串才不丢精度,`rebuild` 时按 `DECIMAL(p,s)` 解析回来;字符串照样可 grep。
- **`timestamptz` —— 带时区时间戳**。业务时间列的一等类型,DuckDB 里是真正的 `TIMESTAMP WITH TIME ZONE`,可用原生日期函数 / 范围查询。文件里存 ISO-8601 带偏移字符串(如 `2026-07-05T12:00:00+00:00`),和引擎自己的 `created_at` / `ds` 一样是 ISO 字符串——可 grep、字典序即时间序。**带时区**是因为 memory system 跨机器 / 时区,裸时间戳会有歧义。
  > 注:引擎代管的 `ds` / `created_at` / `deleted_ds` / `deleted_at` 仍是 VARCHAR ISO(grep 优先的既定选择,§5);业务列可以选 `timestamptz` 拿原生日期能力——两者在文件里都是 ISO 字符串,形态一致。
- **`json` —— 一个逃生舱顶掉所有嵌套**。存任意 JSON(对象 / 数组 / 嵌套),DuckDB 里是 `JSON` 逻辑类型,可用 `json_extract` 等函数查;省得为 list / struct / map / 半结构化元数据各加一个类型。文件里就是**原生嵌套 JSON**——最自然、最可 grep。`json` 列**不能** `searchable`(向量只 embed 文本,§6),也不能做主键。

### 3.2 `primary`:主键(单独字段)

主键**不写进类型字符串**,而是**表级单独字段** `primary`,和 `searchable` / `files` 平级:

```python
"columns": {"card_id": "str", "issue": "str"},
"primary": "card_id",
```

- **每表有且仅有一个**(必填),值是某个已声明列的列名。
- 是**三引擎对齐的锚**:DuckDB 行 / LanceDB 文档 / 文件路径都按它对齐(见 §8)。
- 主键列须是标量 `str` / `int`(不能 `json` / `decimal` / `timestamptz` / `float` / `bool`)。
- 不引入 FK / 复合主键(关系走扁平列 join,容忍悬挂,不加外键)。

## 4. 声明式,不从首行推断(为什么)

类型**来自声明,不看首行数据**。反例:某列首行是 `null`,若从数据推断会把它错判成 string,之后所有数值过滤 / 范围查询全废。searchbase 已踩过这坑(它的 schema 也是声明的)。声明式把这类「首行污染列型」的坑焊死在 `open` 时——坏 schema 当场 `SchemaError`,不拖到运行时。

## 5. 引擎代管的元数据列(不许声明)

schema 只写**业务列**;下面四列引擎自动加,**不许自己声明**(声明了 → `SchemaError`):

| 列 | 含义 |
|---|---|
| `ds` | 创建日 `YYYYMMDD`(分区键) |
| `created_at` | 创建精确时刻 |
| `deleted_ds` | 删除日 `YYYYMMDD`(活行 `NULL`) |
| `deleted_at` | 删除精确时刻(活行 `NULL`) |

这四列驱动**时光机**(见 [time_machine.md](time_machine.md)):可见性谓词 `ds <= D AND (deleted_ds IS NULL OR deleted_ds > D)`。**为什么不让用户碰**:时光机的严谨性靠引擎独占这几列——调用方写不到 = 历史不会被污染,insert-only 的不变性由结构保证、不靠自觉。

## 6. `searchable`:声明「哪列可搜」

- 列出可被 `search()` 语义检索的列;**必须是 `str` 列**(向量只 embed 文本,`json` / 数值 / 时间列不可搜)。声明了 → `insert` 时该列文本**自动 embed** 进向量侧、`search()` **自动查**——写入方 / 查询方都不用碰第二条(向量)栈。
- **有 `searchable` ⇒ 必须注入 embedder**(否则 `open` 时 `EmbedderInvalid`,早失败)。
- **没有 `searchable` 的表 = 纯 DuckDB 表**:零向量开销,连 LanceDB collection 都不建。
- 这就是「`search()` 一等算子」的接线开关:声明与否决定一张表进不进向量侧。

## 7. `files`:声明「落不落文件镜像、怎么落」

- 字符串 = 一行一文件(json,模板含**主键**);字典 = `{"path": …, "mode": "json"|"jsonl"}`。
- **json vs jsonl 由结构判定**(模板含主键 = 1:1 → json;含分组键 = 1:多 → jsonl),**不由 seekbase 猜业务**——判据是结构性的、也是正确性条件(见 [store.md §2](store.md))。
- 声明了 `files` → 写路径自动「文件 → 行 → 向量」三写;没声明 = 无镜像(纯派生 / 日志表)。
- 顶层按 `ds=YYYYMMDD` 日期分区、分区内按主键哈希分片,由引擎自动加,schema 只写逻辑模板(见 [store.md](store.md))。

## 8. 一张 schema 表 → 三引擎的落地

以 `cards`(`issue` 可搜、声明了 files)为例,`open` 时从这一条声明落成:

| 目标 | 落地 |
|---|---|
| **DuckDB** | `CREATE TABLE cards (card_id VARCHAR, issue VARCHAR, kind VARCHAR, ds VARCHAR, created_at VARCHAR, deleted_ds VARCHAR, deleted_at VARCHAR, PRIMARY KEY(card_id))` |
| **LanceDB** | collection `cards`:`issue` 的向量 + 对齐字段(因为 `issue` 是 `searchable`) |
| **files** | `ds=<创建日>/cards/<shard>/card_x.json`(因为声明了 `files`) |

**主键 `card_id` 是三处对齐的锚**:一次 `insert` 三写,DuckDB 行 / LanceDB 文档 / 文件都以它定位,id 对齐由引擎在端口后面兜。

## 9. 校验:`open` 时一次性、早失败

`seekbase.schema.parse_schema` 在 `open` / server 启动时跑一遍:

| 规则 | 违反 → |
|---|---|
| `primary` 字段存在,且指向一个已声明列 | `SchemaError` |
| 主键列类型是 `str` / `int` | `SchemaError` |
| 不许声明 `ds`/`created_at`/`deleted_ds`/`deleted_at` | `SchemaError` |
| 列类型 ∈ `str`/`int`/`float`/`bool`/`decimal(p,s)`/`timestamptz`/`json` | `SchemaError` |
| `decimal` 的 `(p,s)` 合法(`1≤p`,`0≤s≤p`) | `SchemaError` |
| `searchable` 列须是已声明的 `str` 列 | `SchemaError` |
| `files` 模板占位符须是已声明列 | `SchemaError` |
| 有 `searchable` 却无 embedder | `EmbedderInvalid` |

坏形状当场报错,是「声明式」的兑现——运行时不会再因结构问题失败。

## 10. schema 演进(待定)

`_meta.json` 存 **schema 指纹 + embedder dim**,`open` 时比对:

- schema 变更 / dim 变更 → 给明确升级路径。走**蓝绿**(「实例 = 版本」,新名字新实例、业务回填、切过去再删,searchbase 那套)还是 **in-place migration**,dim 变触发 reembed —— 见 DESIGN §10。
- 因为库是**派生层**(canonical 在文件),schema 演进最坏可 `rebuild()` 从文件重灌(见 [store.md](store.md))。

## 11. 与其他文档

- [store.md](store.md):`files` 三写、布局、分区、原子性。
- [time_machine.md](time_machine.md):四个元数据列如何实现时光机。
- [api/setup.md](../api/setup.md#3-声明-schema):对外声明用法(参考)。
