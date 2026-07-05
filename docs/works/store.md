# store — 三写形态设计(files / DuckDB / LanceDB)

> 状态:**M2–M4 已落**(files→行→向量三写:文件 append、DuckDB 派生行、outbox→LanceDB 异步兑现;`rebuild` replay;`vacuum` 按行清死行 + 重写 jsonl)。本文定下三个存储的角色、files 目录结构(**按天分区、每表一个 append 日志**)、insert 的「文件最先」原子性顺序,以及用 files 校准派生层的机制。

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

- **files = canonical(权威)**:数据的真相。纯文本 JSONL、可 `grep`/`cat`/`diff`/git,是可审计、可信任的底线。
- **DuckDB = 结构化派生**:行存取、过滤、聚合、join、`ds` 时间窗可见性。**派生索引,可重建**。
- **LanceDB = 向量派生**:`searchable` 列的 embedding 与 ANN 检索。**派生索引,可重建**。

DuckDB 与 LanceDB 都可从 files 整体重灌(`rebuild()`),坏了 / 丢了都不致命——files 在,库就在。

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
  duck.db                                    # DuckDB:业务行(带 ds/created_at/deleted_ds/deleted_at)+ _outbox
  lance/                                     # LanceDB:每个有 searchable 列的表一个 collection
  _meta.json                                 # 实例元:schema 指纹、版本、embedder dim
```

### 2.1 行 ↔ 文件 映射

- **一条行 = `ds=<写入日>/<表>.jsonl` 里 append 的一行 compact JSON**(全部声明列 + 元数据 `ds`/`created_at`;删除信息是另一条墓碑事件,§5)。
- **定位一行走派生 DuckDB**(按主键索引),不走文件。canonical 点查靠 grep(`grep '"card_id":"card_x"' files/ds=*/cards.jsonl`)——不是 O(1),但点查本就不该扫文件。
- **重复主键 = 再 append 一行**(一个新版本),派生表取最新(latest-wins)——和「改 = 追加新行」的 insert-only 语义天然一致;文件从不回改已写的行。
- **主键(`primary`)保留**,但**不进文件路径 / 不做文件名**:它只在派生层做行标识、latest-wins、删除定位。

### 2.2 为什么这一种形态就够(不要 PK 路径、不要分片)

之前「一行一文件(`<pk>.json`)」会把几百万行炸成几百万个文件,得靠哈希分片兜底。**改成「每表每天一个 jsonl」后,扇出问题从根上消失**:

- **文件数 = 天数 × 表数**,天生有界——不碰 ext/FAT 单目录上限,`ls`/`readdir` 不拖垮,**不需要哈希分片**。
- **`ls files/ds=20260705/`** = 当天写过的所有表;`ls files/` = 所有活跃日期。审计 / 备份 / 回看某天都落成「看一个目录」。
- **时光机 = 分区裁剪**:`query` 的 `ds_start`/`ds_end` 圈定要读的分区区间(只给 `ds_end` = 回到那天及之前;见 [time_machine.md](time_machine.md))。
- **代价**:高频表单个 `<表>.jsonl` 一天可能很大(几 GB)。顺序读(grep/rebuild)没问题、随机访问不需要;真撑不住再加**日内轮转**(`cards.0.jsonl` / `cards.1.jsonl`),但那是 YAGNI。

## 3. insert 的原子性:文件最先

跨引擎没有分布式事务(DuckDB 的事务包不住 LanceDB)。解法不是两阶段提交,而是**定死写入顺序 + 用 canonical 校准**。`insert(row)` 三步:

```
① files 最先 —— append 落盘
     以 O_APPEND 打开 ds=今天/<表>.jsonl → 写一行 `json.dumps(record)+"\n"` → fsync
        │  (写被单写者串行化,DESIGN §6.4;append 不交错)
        ▼
② 一个 DuckDB 事务(原子)
     写业务行 + 追加 _outbox 一行(向量作业)     ← 队列就在 DuckDB 里,和业务行同事务
        │  commit
        ▼
③ 后台 consumer(commit 后异步)
     取 pending → embed → LanceDB upsert → 标 done
```

为什么文件最先:**canonical 先落地**,后面每步都是从它派生。任一后续步骤失败,files 都是那条数据的真相,可据以校准(§4)。

原子性 / 无锁读的两条保证:

- **过去日期的 jsonl 永不再动;当天的只 append**(只增长、不回改已写的行)——读者拿到的历史稳定、可缓存。
- **单写者串行化 append**:seekbase 的写都过同一写线程(DESIGN §6.4),同一文件的追加不并发交错。
- **torn append 容忍**:crash 时最后一行可能写半截。读侧只认完整 JSON 行、丢尾部残行;而残行对应的 row 也没提交进 DuckDB(②在①之后),两边不分叉(§4)。

## 4. 一致性关系与校准:file ≥ row ≥ vector

写入顺序(files → row → vector)决定三个面的新旧关系**固定可推理**:

```
file 面  ≥  row 面(DuckDB)  ≥  vector 面(LanceDB)
```

- **file 面永不缺数据**,至多瞬时超前 row 一步(崩溃后由 repair 收敛)。
- **row 面强一致**(不带 `search()` 的查询永远读到已兑现的行)。
- **vector 面最终一致**(consumer 异步兑现)。

崩溃发生在哪一步,都能用 files 校准:

| 崩溃点 | 现象 | 校准 |
|---|---|---|
| ① 之后、② 之前 | jsonl 有、行没有 | **repair**:replay 未落库的行 + 补 outbox 作业 |
| ② 之后、③ 之前 | 行有、向量没有 | outbox 里仍有 pending,consumer 重启 **replay**(按 id upsert 幂等) |
| 任意时刻整体损坏 | 派生层不可信 | **rebuild()**:按 `ds` 顺序 replay 全部 `<表>.jsonl` → 重灌 DuckDB + 重入队向量 |

- **`repair`(open 时轻量自检)**:`file ≥ row` 不变式若被打破,从 files replay 补齐。
- **`rebuild()`(显式重建)**:整体从 files 重灌双引擎(见 [api/admin.md](../api/admin.md))。

对账也靠 files:DuckDB 原生把 JSONL 当外部表读——`SELECT * FROM read_json('files/ds=*/cards.jsonl', format='newline_delimited', hive_partitioning=true)`(`ds` 自动成为一列),直接与 row 面 diff。

## 5. delete / 墓碑:删除日分区里的一条事件

`delete()` 打墓碑,**不删也不改任何已写的行**——删除是一条**append 事件**,两半:

- **canonical**:往 `ds=删除日/<表>.jsonl` **append 一条墓碑记录** `{"_deleted": "<pk>", "deleted_at": "…"}`。纯 append,历史文件永不回改。
- **派生 DuckDB**:按主键定位那行,置 `deleted_ds`(= 删除日)/ `deleted_at`。

要点:

- **时光机天然兜住删除**:墓碑落删除日分区,as-of 早于删除日的查询(读 `ds <= D` 分区)看不到这条墓碑 → 那行仍可见。可见性判定 `ds <= D AND (deleted_ds IS NULL OR deleted_ds > D)`——完备设计见 [time_machine.md](time_machine.md)。
- **rebuild 保真**:按 `ds` 顺序 replay,insert 行建行、`_deleted` 行按主键置 `deleted_ds`。
- **grep 看得见**:`grep _deleted files/ds=20260706/cards.jsonl` 就是当天删了什么。

真正物理删只发生在显式 `vacuum(before=D)`:**按行**清 `deleted_ds < D` 的死行——重写受影响的 `<表>.jsonl`、丢掉那些行的 insert / 墓碑事件(整表整天全死就删掉文件),同步清派生行 + 向量。**不是**整块删分区(会误删「早创建但仍活」的行)。活行、删于 `≥ D` 的行都保留;代价:vacuum 后只能倒带到 `≥ D`。见 [time_machine.md §8](time_machine.md)。

## 6. 与 file-canonical 的关系

file-canonical 模式不变、且更彻底:文件仍是权威,数据库仍是派生索引。变的是**谁维护文件**——从「各 store 手写文件 ops」收进 seekbase:**每张表自动镜像**成按天分区的 `<表>.jsonl`(不需要 schema 声明落哪、怎么落),写路径自动「文件 → 行 → 向量」三写,谁也不会忘了哪一边。
