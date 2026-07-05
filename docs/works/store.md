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

### 2.1 两种模式,由 schema 声明(不由 seekbase 猜)

**由 schema 声明,不由 seekbase 猜**——seekbase 业务无关,不知道「这是 rounds 所以该 append」。判据是**纯结构性**的、也是正确性条件:路径模板和行是 1:1 还是 1:多。

- **json 模式(一行一文件)** —— 模板 **1:1 于行**(含**主键**):每行解析出唯一路径、独占一个文件。per-row 文件必须由唯一键定位,否则两行撞同一文件——所以模板**必须含主键**。
- **jsonl 模式(追加)** —— 模板 **1:多于行**(按**非主键分组列**):同组多行共享一个文件。键不唯一 → 只能 append。

> 一句话:**模板里放主键 = json,放分组键 = jsonl**。seekbase 能据此自动判,为消歧仍让它显式声明(或由 `.json`/`.jsonl` 扩展名给)。没声明 `files` 的表 = 无镜像。

### 2.2 行 ↔ 文件 映射(讲清楚)

一条 DB 行到磁盘的对应关系,两模式各自定死:

| | json 模式 | jsonl 模式 |
|---|---|---|
| **路径怎么来** | 模板 `{col}` 用该行列值填充 + PK 哈希分片(§2.3)| 同左,但填充用**分组列**,分片按分组键 |
| **一个文件装什么** | **这一行**的完整 JSON 对象(全部声明列 + `created_at`/`deleted_at`),pretty-print、键序稳定 | 同组**多行**,**一行一条** compact JSON(各含自己的元数据列),按 insert 顺序 append |
| **怎么定位一行** | 由 PK 直接算出路径 → 打开那一个文件(O(1)) | 由分组列算出文件 → 顺扫其行、按 PK 命中(同 PK 多条时**最后一条为准**)|
| **删除(墓碑)** | 就地把 `deleted_at` 写回该文件(唯一一次重写,temp+rename)| append 一条墓碑记录 `{"_deleted": "<pk>", "deleted_at": "…"}` |
| **文件名安全** | 文件名用 PK 值;含 `/`、超长、非法字符的 PK 由 seekbase **百分号编码**后再落地 | 分组键同样编码 |

要点:**文件里存的是「行本身」的完整快照**(不是指针、不是片段),所以任一文件单独拿出来就能读懂一行/一组;这也是 §4 能用 files 校准派生层的前提。

### 2.3 规模:目录扇出与分片(别让一个目录塞几十万文件)

**问题**:json 模式一行一文件,若平铺成 `cards/{card_id}.json`,几百万行 = 几百万个文件挤在 `cards/` 一个目录里。这会**爆**——不只是慢:

- ext3/ext4 单目录**子目录数**受 link-count 限(~64k);
- FAT/exFAT 单目录条目上限 65534;
- 即便文件数不撞硬限,几十万条目平铺一目录,`readdir`/`ls`/tab 补全/`glob` 也统统拖垮。

**解法:按 PK 哈希分两级分片**(git object store 的路数),seekbase 自动加、业务无感:

```
<data_dir>/
  files/
    cards/                                   # json:按 PK 哈希分片,单目录条目恒定有界
      3f/                                    # = hash(card_id)[0:2]
        a1/                                  # = hash(card_id)[2:4]
          card_x.json                        # 该行的完整 JSON
    sessions/                                # jsonl:分组键多时同样分片
      9c/ e2/
        sess_1/
          rounds.jsonl                       # 同 session 的多行 append 进来
  duck.db                                    # DuckDB:业务行 + _outbox 队列表
  lance/                                     # LanceDB:每个有 searchable 列的表一个 collection
  _meta.json                                 # 实例元:schema 指纹、版本、embedder dim
```

- 分片键 = `blake2b(主键值)` 的前 4 个 hex,拆成两级各 256 路 → **65536 个叶目录**;每级中间目录恒 ≤256 个子目录,叶目录约 `N/65536` 个文件。
  - N=1000 万 → 每叶 ~150 个文件;N=1 亿 → ~1500。**任何单目录条目数都保持有界**,不碰 FS 上限、不拖垮 readdir。
- **仍可 grep / 定位**:给定 id 就能算出它的分片路径(O(1) 打开);`grep -r files/cards/` 整树递归照跑。只有「`ls cards/` 平铺看全部」这一种用法变成了走两层——换来不炸目录。
- **jsonl 模式天然少扇出**(多行归一文件),但**分组键基数很大**(如百万 session)时,存放这些 `*.jsonl` 的目录同样会爆 → 对分组键套同一分片。
- 分片深度(1 级 256 / 2 级 65536)按预期规模可调,记在 `_meta.json`,`rebuild()` 一致复算。

## 3. insert 的原子性:文件最先

跨引擎没有分布式事务(DuckDB 的事务包不住 LanceDB)。解法不是两阶段提交,而是**定死写入顺序 + 用 canonical 校准**。`insert(row)` 三步:

```
① files 最先 —— 原子落盘(目标路径含 §2.3 分片,按需先 mkdir -p 分片目录)
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
