# store — 两层存储设计(files canonical / DuckDB 派生)

> 状态:**已落**(files→行→检索:文件 append、DuckDB 单表行 + 就地 vss/fts 索引;`rebuild` replay)。**单引擎:结构化 + 向量 + 全文全在一个 `duck.db`,没有 LanceDB。写入同步、主键写一次(重复报错);delete 是软删墓碑、无物理删 / vacuum**(文件真·纯 append,零例外)。本文定下两层存储的角色、files 目录结构(**按天分区、每表一个 append 日志**)、insert 的「文件最先」原子性顺序,以及用 files 校准派生层的机制。

## 1. 两层存储,一个端口

一次写入落到两层:canonical 文件 + 派生的 DuckDB(单文件,**每张业务表一张物理表**,行里就带向量 / 全文列)。

```
                        Seekbase(端口)
          ┌─────────────────┴─────────────────┐
       files                          DuckDB(duck.db 单文件)
    (canonical)                每业务表一张物理表 _sb_<表>
   可 grep/diff          业务列 + ds/created_at/deleted_ds/deleted_at
                        + 每个可搜列的 _vec_<列>(vss)/ _tok_<列>(fts)
          └────────── 派生自 files,可从 files 重建 ──────────┘
```

- **files = canonical(权威)**:数据的真相。纯文本 JSONL、可 `grep`/`cat`/`diff`/git,是可审计、可信任的底线。
- **DuckDB = 派生(单引擎)**:每张业务表一张物理表 `_sb_<表>`,可从 files 重建。一行 = 一个主键(**写一次**),行里同时带:
  - 业务列 + 引擎元数据(`ds`/`created_at`/`deleted_ds`/`deleted_at`)——过滤、聚合、join、`ds` 时间窗都在这张表上。
  - 每个 `searchable` 列的 `_vec_<列> FLOAT[dim]`(`vss`/HNSW 索引,向量语义)+ `_tok_<列>`(`fts`/BM25 索引,全文;中文经 jieba 分词)。`search(列, …)` 直接在这张表上 hybrid 融合。**这就是原来 LanceDB 的角色**,现在收进同一张表、同一个 `duck.db`。

整个 DuckDB 都可从 files 整体重灌(`rebuild()`),坏了 / 丢了都不致命——files 在,库就在。**单文件让打开的 fd 数恒定**,避开 LanceDB 版本化碎片文件的 EMFILE/fd 耗尽(见 [search.md](search.md))。

## 2. files 目录结构:按天分区,每表一个 append 日志

**只有一种形态**:顶层按日期分区 `ds=YYYYMMDD`(写入日),每张表在分区里一个 append-only 的 `<表>.jsonl`。当天写过这张表就有这个文件,没写就没有。

```
<data_dir>/
  files/
    ds=20260705/                             # 顶层 = 日期分区(写入日)
      cards.jsonl                            # 当天写过 cards 就有;一行一条 append
      rounds.jsonl
    ds=20260706/                             # 当天没写 rounds,就没 rounds.jsonl
      cards.jsonl
  duck.db                                    # DuckDB 单文件:每业务表一张物理表
                                             #   (业务列 + ds/created_at/deleted_ds/deleted_at
                                             #    + 每可搜列 _vec_<列>/_tok_<列>,vss+fts 就地索引)
  duck.db.wal                                # DuckDB WAL
```

### 2.1 行 ↔ 文件 映射

- **一条行 = `ds=<写入日>/<表>.jsonl` 里 append 的一行 compact JSON**(全部声明列 + 元数据 `ds`/`created_at`;删除信息是另一条墓碑事件,§5)。
- **定位一行走派生 DuckDB**(按主键索引),不走文件。canonical 点查靠 grep(`grep '"card_id":"card_x"' files/ds=*/cards.jsonl`)——不是 O(1),但点查本就不该扫文件。
- **主键写一次**:`_sb_<表>` 上主键是 `PRIMARY KEY`,**重复主键再写 = 报错**(insert-only、不覆盖);DuckDB 表里因此**一个主键恰好一行**。文件侧则是纯 append(一次 insert 一行、一次 delete 一条墓碑),历史永不回改。
- **主键(`primary`)保留**,但**不进文件路径 / 不做文件名**:它只在派生层做行标识、按主键定位、删除定位。

### 2.2 为什么这一种形态就够(不要 PK 路径、不要分片)

之前「一行一文件(`<pk>.json`)」会把几百万行炸成几百万个文件,得靠哈希分片兜底。**改成「每表每天一个 jsonl」后,扇出问题从根上消失**:

- **文件数 = 天数 × 表数**,天生有界——不碰 ext/FAT 单目录上限,`ls`/`readdir` 不拖垮,**不需要哈希分片**。
- **`ls files/ds=20260705/`** = 当天写过的所有表;`ls files/` = 所有活跃日期。审计 / 备份 / 回看某天都落成「看一个目录」。
- **时光机 = 分区裁剪**:`query` 的 `ds_start`/`ds_end` 圈定要读的分区区间(只给 `ds_end` = 回到那天及之前;见 [time_machine.md](time_machine.md))。
- **代价**:高频表单个 `<表>.jsonl` 一天可能很大(几 GB)。顺序读(grep/rebuild)没问题、随机访问不需要;真撑不住再加**日内轮转**(`cards.0.jsonl` / `cards.1.jsonl`),但那是 YAGNI。

## 3. insert 的原子性:文件最先

写入是**同步**的(向量在 insert 时就地 embed 好、随行落库),但仍先落 canonical。解法不是两阶段提交,而是**定死写入顺序 + 用 canonical 校准**。`insert(row)` 三步:

```
① 校验 —— 主键不得已存在(已存在 → QueryError,什么都不写)
     inline embed 每个可搜列的文本 → 向量;jieba 分词 → tok
        │
② files 最先 —— append 落盘
     以 O_APPEND 打开 ds=今天/<表>.jsonl → 写一行 `json.dumps(record)+"\n"` → fsync
        │  (写被单写者串行化,DESIGN §6.4;append 不交错)
        ▼
③ 一个 DuckDB 事务(原子)
     INSERT 一行(业务 + ds/created_at + _vec_<列>/_tok_<列>)→ 重建该表 FTS 索引
        │  commit
        ▼
   ticket 立即 done(向量已随行落库,没有异步兑现)
```

为什么文件最先:**canonical 先落地**,后面每步都是从它派生。任一后续步骤失败,files 都是那条数据的真相,可据以校准(§4)。

原子性 / 无锁读的两条保证:

- **过去日期的 jsonl 永不再动;当天的只 append**(只增长、不回改已写的行)——读者拿到的历史稳定、可缓存。
- **单写者串行化 append**:seekbase 的写都过同一写线程(DESIGN §6.4),同一文件的追加不并发交错。
- **torn append 容忍**:crash 时最后一行可能写半截。读侧只认完整 JSON 行、丢尾部残行;而残行对应的 row 也没提交进 DuckDB(③在②之后),两边不分叉(§4)。

## 4. 一致性关系与校准:file ≥ DuckDB

写入顺序(files → DuckDB 行)决定两个面的新旧关系**固定可推理**:

```
file 面  ≥  DuckDB 面(行 + vss + fts,同一事务落地)
```

- **file 面永不缺数据**,至多瞬时超前 DuckDB 一步(崩溃后由 repair 收敛)。
- **DuckDB 面强一致且检索同步**:因为向量在 insert 时就地 embed、随行同事务落库、FTS 同步重建——**没有异步兑现窗口**,`insert` 返回即 `search()` 可见(结构化查询本就强一致)。

崩溃发生在哪一步,都能用 files 校准:

| 崩溃点 | 现象 | 校准 |
|---|---|---|
| ② 之后、③ 之前 | jsonl 有、DuckDB 没有 | **repair**:replay 未落库的行(重新 embed + INSERT) |
| 任意时刻整体损坏 | 派生层不可信 | **rebuild()**:按 `ds` 顺序 replay 全部 `<表>.jsonl` → 清空并重灌物理表(重新 embed + 重建索引) |

- **`repair`(open 时轻量自检)**:`file ≥ DuckDB` 不变式若被打破,从 files replay 补齐。
- **`rebuild()`(显式重建)**:整体从 files 重灌 DuckDB(清空物理表,replay 重填、重新 embed、重建 vss/fts;见 [api/admin.md](../api/admin.md))。

对账也靠 files:DuckDB 原生把 JSONL 当外部表读——`SELECT * FROM read_json('files/ds=*/cards.jsonl', format='newline_delimited', hive_partitioning=true)`(`ds` 自动成为一列),直接与 DuckDB 面 diff。

## 5. delete / 墓碑:软删标记 + 一条文件事件

`delete()` 打墓碑,**不删任何行**——删除是一条**append 文件事件** + 派生行上的一次**软删标记**:

- **canonical**:往 `ds=删除日/<表>.jsonl` **append 一条墓碑记录** `{"_deleted": "<pk>", "deleted_at": "…"}`。纯 append,历史文件永不回改。
- **派生 DuckDB**:对那一行 **`UPDATE deleted_ds=删除日, deleted_at=…`**(软删标记)——只动非索引的元数据列,行仍在。
- **检索**:`search()` 的候选子句带 `deleted_ds IS NULL`,软删的行不再被带出(其 `_tok` 还在 BM25 索引里,但被这条谓词裁掉)。

要点:

- **时光机天然兜住删除**:软删只记 `deleted_ds`;as-of 早于删除日 → `deleted_ds > ds_end` → 那行仍可见。判定是一句可下推的谓词,见 [time_machine.md](time_machine.md)。
- **rebuild 保真**:按 `ds` 顺序 replay,put→INSERT 行、`_deleted`→对该行 `UPDATE deleted_ds`(顺序保住)。
- **grep 看得见**:`grep _deleted files/ds=20260706/cards.jsonl` 就是当天删了什么。

**没有物理删、没有 vacuum**:`delete` 永远只是软删标记 + 一条墓碑文件事件,被删的行永久留着——文件真·纯 append,一次都不回改。空间单调增长,memory 规模可接受;换来「历史永不丢」。见 [time_machine.md §8](time_machine.md)。

## 6. 与 file-canonical 的关系

file-canonical 模式不变、且更彻底:文件仍是权威,DuckDB 仍是派生索引。变的是**谁维护文件**——从「各 store 手写文件 ops」收进 seekbase:**每张表自动镜像**成按天分区的 `<表>.jsonl`(不需要 schema 声明落哪、怎么落),写路径自动「文件 → DuckDB 行(含 vss+fts)」,谁也不会忘了哪一边。检索这一层从进程外的 LanceDB 收进同一张 DuckDB 表,少一个引擎、少一套 fd 运维(见 [search.md](search.md))。
