# schema — 声明式表结构(设计)

> 状态:设计。schema 是 seekbase 的**单一声明源**:声明一次,DDL / 检索派生(vss+fts)/ 文件镜像 / 日期分区 / 时光机字段全部从它推导。对外声明用法见 [api/setup.md](../api/setup.md#3-声明-schema);本文讲设计与推导。

## 1. 定位:一处声明,处处推导

schema 描述每张表的**列、哪些列可语义搜**。它是 seekbase 唯一需要人手写的结构定义;从这一处,自动推导出:

```
                         SCHEMA(唯一声明源)
        ┌───────────────┬───────────────┬───────────────┐
   DuckDB 物理表     检索后端派生      files 镜像      元数据列
   (结构化 DDL)      (若有 searchable)  (canonical)     ds/created_at/
   建表/列/主键      duck-vss:_vec/_tok (每表自动)      deleted_ds/deleted_at
                     或 LanceDB 独立库                  → 时光机
```

上层(repository / service)从此**不手写 SQL、不手写文件 ops、不手写检索接线**——加一列 = 改一处 schema,DDL / 迁移 / 文件+检索派生全由 seekbase 兑现。检索(向量 + 全文 hybrid)由**可插拔后端**兑现(duck-vss 就地长在业务表 / LanceDB 独立库,见 [search.md](search.md)),`searchable` 声明与否决定建不建它;检索通过管道的 `search` source 段暴露,**不是 SQL 函数**(见 [pipeline-as-anything.md](pipeline-as-anything.md))。文件镜像是**每张表自动**落成按天分区的 `<表>.jsonl`,不用在 schema 里声明(见 [store.md](store.md))。

## 2. 声明格式

**SCHEMA 是有序列表**(不是按表名 key 的 dict),表名是 `table` 字段;列也是有序列表:

```python
SCHEMA = [
    {
        "table": "cards",                            # 表名(字段,不是 dict key)
        "columns": [                                 # 有序列表,列顺序 = DDL 列顺序
            {"name": "card_id", "type": "str"},
            {"name": "issue",   "type": "str"},
            {"name": "kind",    "type": "str"},
        ],
        "primary": "card_id",                        # 主键列名
        "searchable": ["issue"],                     # 可选:哪些列可被 search source 段检索
    },
]
```

一个表项的字段:`table`(必)、`columns`(必)、`primary`(必)、`searchable`(可选)。**没有 `files` 字段**——镜像每表自动(§1 / [store.md](store.md))。

> **为什么用列表、不用 kv dict**:顺序是**显式**的——建表顺序 = 列表顺序(将来若有建表依赖 / 顺序,直接照列表走,不靠 dict 插入顺序这种隐式、跨语言 / JSON 不保证的东西);列顺序同理 = DDL 列顺序。表名、列名都做**字段**,不做 key。

## 3. `columns`(有序列表:每项 `{name, type}`)

`columns` 是**有序列表**,每项 `{"name": …, "type": …}`,**不掺修饰**——列顺序 = DDL 列顺序;主键单独走 `primary` 字段(§3.2)。下表是 `type` 的取值:

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

前四种是标量;另三种按一条底线挑出来——**能否干净塞进 JSONL 一行 + DuckDB(结构化 + 向量列)往返、且可 grep**。所以 `blob` 等二进制**不收**(base64 进 JSON 又丑又不可 grep,违背 canonical 是文本的底线)。

- **`decimal(p,s)` —— 精确定点**。给精度 `p`、标度 `s`(如 `decimal(18,2)`)。用于金额、带样本量的分数等 `float`(有损二进制浮点)表达不了的值。**文件里存成字符串**(`"123.45"`)——JSON 只有浮点数,存字符串才不丢精度,`rebuild` 时按 `DECIMAL(p,s)` 解析回来;字符串照样可 grep。
- **`timestamptz` —— 带时区时间戳**。业务时间列的一等类型,DuckDB 里是真正的 `TIMESTAMP WITH TIME ZONE`,可用原生日期函数 / 范围查询。文件里存 ISO-8601 带偏移字符串(如 `2026-07-05T12:00:00+00:00`),和引擎自己的 `created_at` / `ds` 一样是 ISO 字符串——可 grep、字典序即时间序。**带时区**是因为 memory system 跨机器 / 时区,裸时间戳会有歧义。
  > 注:引擎代管的 `ds` / `created_at` / `deleted_ds` / `deleted_at` 仍是 VARCHAR ISO(grep 优先的既定选择,§5);业务列可以选 `timestamptz` 拿原生日期能力——两者在文件里都是 ISO 字符串,形态一致。
- **`json` —— 一个逃生舱顶掉所有嵌套**。存任意 JSON(对象 / 数组 / 嵌套),DuckDB 里是 `JSON` 逻辑类型,可用 `json_extract` 等函数查;省得为 list / struct / map / 半结构化元数据各加一个类型。文件里就是嵌套 JSON(在那一行里)——最自然、最可 grep。`json` 列**不能** `searchable`(向量只 embed 文本,§6),也不能做主键。

### 3.2 `primary`:主键(单独字段)

主键**不写进类型字符串**,而是**表级单独字段** `primary`,和 `searchable` 平级:

```python
"columns": [{"name": "card_id", "type": "str"}, {"name": "issue", "type": "str"}],
"primary": "card_id",
```

- **每表有且仅有一个**(必填),值是某个已声明列的 `name`。
- 是**各层对齐的锚**:DuckDB 行 / 文件里的行都按它对齐(见 §7)。文件里主键**不做文件名 / 不进路径**——只在派生层做行标识、按主键定位、删除定位(见 [store.md §2.1](store.md))。
- **主键写一次**:`_sb_<表>` 上主键是 `PRIMARY KEY`,重复主键再 insert 直接报错(insert-only、不覆盖),故一个主键恰好一行。
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

这四列驱动**时光机**(见 [time_machine.md](time_machine.md)):每个主键在 `_sb_<表>` 里恰好一行,`query` 按一句 `ds` 谓词现算「as-of D 是否可见」(`ds <= D AND (deleted_ds IS NULL OR deleted_ds > D)`)。**为什么不让用户碰**:时光机的严谨性靠引擎独占这几列——调用方写不到 = 历史不会被污染,insert-only 的不变性由结构保证、不靠自觉。

## 6. `searchable`:声明「哪列可搜」

- 列出可被 `search` source 段检索的列;**必须是 `str` 列**(向量只 embed 文本、BM25 只索引文本,`json` / 数值 / 时间列不可搜)。声明了 → 该列进**检索后端**(duck-vss:该表多出 `_vec_<列>`/`_tok_<列>` 两列 + 索引;LanceDB:进独立库);`insert` 时该列文本**自动 embed + jieba 分词**随行落库、`search cards '…'` **自动 hybrid 查**——写入方 / 查询方都不用碰检索栈(见 [search.md](search.md))。
- **有 `searchable` ⇒ 必须注入 embedder**(否则 `open` 时 `EmbedderInvalid`,早失败)。
- **没有 `searchable` 的表 = 纯结构化表**:零检索开销,检索后端不为它建任何索引。
- 这就是检索后端的接线开关:声明与否决定一张表**进不进检索后端**(`search` source 段能不能搜它)。

## 7. 一张 schema 表 → 落地(结构化 DuckDB + 可插拔检索后端 + 文件)

以 `cards`(`issue` 可搜)为例,`open` 时从这一条声明落成:

| 目标 | 落地 |
|---|---|
| **DuckDB 物理表** | `CREATE TABLE _sb_cards (card_id VARCHAR, issue VARCHAR, kind VARCHAR, ds, created_at, deleted_ds, deleted_at, _vec_issue FLOAT[dim], _tok_issue VARCHAR, PRIMARY KEY(card_id))` —— **一主键一行、写一次**(重复主键报错);检索列就长在这张表上 |
| **检索后端**(以 duck-vss 为例) | 每个 `searchable` 列一个 HNSW 索引在 `_vec_<列>`(cosine);一个 BM25 索引盖住所有 `_tok_<列>`,`search cards 'issue…'` 用 `match_bm25(fields:=)` 限定该列、hybrid 融合(LanceDB 后端则进独立库;见 [search.md §5](search.md)) |
| **files** | `ds=<写入日>/cards.jsonl`,每次写 append 一行(每表自动,见 [store.md](store.md)) |

**主键 `card_id` 是各层对齐的锚**:一次 `insert`,DuckDB 行 / 文件里的行都以它对齐,id 对齐由引擎在端口后面兜。

**顺序显式**:DuckDB 列顺序 = `columns` 列表顺序;建表顺序 = `SCHEMA` 列表顺序(将来有建表依赖就照此走)。

## 8. 校验:`open` 时一次性、早失败

`seekbase.schema.parse_schema` 在 `open` / server 启动时跑一遍:

| 规则 | 违反 → |
|---|---|
| `SCHEMA` 是列表;每项有 `table`,表名唯一 | `SchemaError` |
| `columns` 是列表;每项有 `name` + `type`,列名表内唯一 | `SchemaError` |
| `primary` 字段存在,且指向一个已声明列 | `SchemaError` |
| 主键列类型是 `str` / `int` | `SchemaError` |
| 不许声明 `ds`/`created_at`/`deleted_ds`/`deleted_at` | `SchemaError` |
| 列类型 ∈ `str`/`int`/`float`/`bool`/`decimal(p,s)`/`timestamptz`/`json` | `SchemaError` |
| `decimal` 的 `(p,s)` 合法(`1≤p`,`0≤s≤p`) | `SchemaError` |
| `searchable` 列须是已声明的 `str` 列 | `SchemaError` |
| 有 `searchable` 却无 embedder | `EmbedderInvalid` |

坏形状当场报错,是「声明式」的兑现——运行时不会再因结构问题失败。

## 9. schema 演进(待定)

`_meta.json` 存 **schema 指纹 + embedder dim**,`open` 时比对:

- schema 变更 / dim 变更 → 给明确升级路径。走**蓝绿**(「实例 = 版本」,新名字新实例、业务回填、切过去再删,searchbase 那套)还是 **in-place migration**,dim 变触发 reembed —— 见 DESIGN §10。
- 因为库是**派生层**(canonical 在文件),schema 演进最坏可 `rebuild()` 从文件重灌(见 [store.md](store.md))。

## 10. 与其他文档

- [store.md](store.md):两层存储(文件 canonical / DuckDB 派生)、原子性。
- [search.md](search.md):`searchable` 如何在业务表上接线 `_vec`/`_tok` 列 + vss/fts 索引、hybrid 检索。
- [time_machine.md](time_machine.md):四个元数据列如何实现时光机。
- [api/setup.md](../api/setup.md#3-声明-schema):对外声明用法(参考)。
