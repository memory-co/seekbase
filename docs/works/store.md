# store — 三写形态设计(files / DuckDB / LanceDB)

> 状态:设计(落地在 M2 文件镜像 + M3 向量/outbox)。本文定下三个存储的角色、files 目录结构、以及 insert 的「文件最先」原子性顺序与校准机制。

## 1. 三个存储,一个端口

一次写入落到三处,各司其职:

```
                    Seekbase(端口)
        ┌───────────────┼───────────────┐
     files            DuckDB            LanceDB
   (canonical)      (结构化行)         (向量)
   可 grep/diff     过滤·聚合·join      embed·ANN
        └────── 派生自 files,可从 files 重建 ──────┘
```

- **files = canonical(权威)**:一行数据的真相。纯文本、可 `grep`/`cat`/`diff`/git,是可审计、可信任的底线。
- **DuckDB = 结构化派生**:行存取、过滤、聚合、join、`as_of` 可见性。**派生索引,可重建**。
- **LanceDB = 向量派生**:`searchable` 列的 embedding 与 ANN 检索。**派生索引,可重建**。

DuckDB 与 LanceDB 都可以从 files 整体重灌(`rebuild()`),所以它们坏了 / 丢了都不致命——files 在,库就在。

## 2. files 目录结构规划

在实例目录下,`files/` 独立成树,按 schema 的 `files` 声明落盘:

```
<data_dir>/
  files/                                   # canonical 镜像(可 grep/diff/git/整目录拷贝备份)
    cards/
      card_x.json                          # json 模式:一行一文件
      card_y.json
    sessions/
      sess_1/
        rounds.jsonl                       # jsonl 模式:append-only 流水
      sess_2/
        rounds.jsonl
  duck.db                                  # DuckDB:业务行 + _outbox 队列表
  lance/                                   # LanceDB:每个有 searchable 列的表一个 collection
  _meta.json                               # 实例元:schema 指纹、版本、embedder dim
```

两种镜像模式,由 schema 的 `files` 声明选(见 [api/schema.md](../api/schema.md)):

- **json 模式** —— `files: "cards/{card_id}.json"`
  - 路径模板用列值填充,**一行一文件**,目录按 id 可导航。
  - 内容是 **pretty-printed、键序稳定** 的 JSON——写一次就不动(引擎 insert-only),diff 友好。
  - 适合「每个名词一条、需要按 id 定位」的表(cards / insights…)。

- **jsonl 模式** —— `files: {path: "sessions/{session_id}/rounds.jsonl", mode: "jsonl"}`
  - append-only,**一行一条**追加,不炸成十万个小文件。
  - 适合流水型大表(rounds…),按某个分组键(session_id)分文件。

没声明 `files` 的表 = 无镜像(纯派生的中间表 / 日志表不必落盘)。

## 3. insert 的原子性:文件最先

跨引擎没有分布式事务(DuckDB 的事务包不住 LanceDB)。解法不是两阶段提交,而是**定死写入顺序 + 用 canonical 校准**。`insert(row)` 三步:

```
① files 最先 —— 原子落盘
     json:  写 temp 文件(完整 JSON)→ fsync → rename 到目标路径(rename 原子)
     jsonl: 以 append 打开目标 → 追加一行 `json.dumps(record)+"\n"` → fsync
        │
        ▼
② 一个 DuckDB 事务(原子)
     写业务行 + 追加 _outbox 一行(向量作业)     ← 队列就在 DuckDB 里,和业务行同事务
        │  commit
        ▼
③ 后台 consumer(commit 后异步)
     取 pending → embed → LanceDB upsert → 标 done
```

为什么文件最先:**canonical 先落地**,后面每一步都是从它派生。任一后续步骤失败,files 都是那条数据的真相,可据以校准(§4)。

原子落盘的两条保证(也是 file 面能被并发无锁读的底子):

- **temp + rename**:并发读永远看到**完整文件**,不存在半截 JSON。
- **insert-only**:已存在的文件不再变(墓碑那次除外),读者拿到的结果稳定、可缓存。

> jsonl 的追加不是 rename 级原子:极端情况下最后一行可能写半截。读侧**容忍**——只认完整的 JSON 行,尾部残行丢弃;而残行对应的 row 也没写上(②在①之后),两边不会分叉(§4)。

## 4. 一致性关系与校准:file ≥ row ≥ vector

写入顺序(files → row → vector)决定了三个面的新旧关系**固定可推理**:

```
file 面  ≥  row 面(DuckDB)  ≥  vector 面(LanceDB)
```

- **file 面永不缺数据**,至多瞬时超前 row 一步(崩溃后由 repair 收敛)。
- **row 面强一致**(不带 `search()` 的查询永远读到已写入的行)。
- **vector 面最终一致**(consumer 异步兑现;要读己之写用 `flush()`)。

崩溃发生在哪一步,都能用 files 校准:

| 崩溃点 | 现象 | 校准 |
|---|---|---|
| ① 之后、② 之前 | 文件有、行没有 | **repair**:通读 files 补 DuckDB 行 + 补 outbox 作业 |
| ② 之后、③ 之前 | 行有、向量没有 | outbox 里仍有 pending,consumer 重启 **replay**(按 id upsert 幂等,重放无害) |
| 任意时刻整体损坏 | 派生层不可信 | **rebuild()**:通读 files → 重灌 DuckDB + 重新入队全部向量 |

两个校准动作:

- **`repair`(open 时轻量自检)**:`file ≥ row` 不变式若被打破,从 files 补齐。
- **`rebuild()`(显式重建)**:整体从 files 重灌双引擎(见 [api/sql-and-admin.md](../api/sql-and-admin.md))。

对账也靠 files:DuckDB 原生 `read_json` / glob 能把 files 当外部表,直接与 row 面 diff(`SELECT * FROM read_json('files/cards/**/*.json')`)。

## 5. delete / 墓碑在 files 里

`delete()` 只打墓碑,files 侧同规矩、**不删文件**:

- **json 模式**:把 `deleted_at` 写回该 JSON(引擎唯一一次代管的重写,同样 temp+rename 原子)。`grep` 也能看到「它曾存在、何时删」。
- **jsonl 模式**:追加一条墓碑记录(如 `{"_deleted": "<id>", "deleted_at": "…"}`);repair/rebuild 按顺序应用。

真正物理删只发生在显式 `vacuum(before=T)`——那才删 files 里 T 之前的墓碑文件/记录,且**明说这是在丢历史**。

## 6. 与 file-canonical 的关系

file-canonical 模式不变、且更彻底:文件仍是权威,数据库仍是派生索引。变的是**谁维护文件**——从「各 store 手写文件 ops」收进 seekbase:schema 里 `files` 声明一次,写路径自动「文件 → 行 → 向量」三写,谁也不会忘了哪一边。
