# time_machine — 用 `ds` 日期分区实现时光机

> 状态:**已落**(可见性谓词 + `ds_start`/`ds_end` 裁剪)。本文把「时光机」用 `ds` 分区讲清:需要**两对**日期字段(创建 / 删除),给出可见性判定、`ds_start`/`ds_end` 的完整语义、真值表、单表落地、以及「无物理删」的取舍。**主键写一次**(重复报错),故一个主键恰好一行——时光机是这一行上的 `ds` 过滤,不涉及多版本重放。存储与文件布局见 [store.md](store.md)。

## 1. 目标

时光机 = 把库倒回某一天 `D`,只看见**「D 那天结束时」存在**的行。判据:

> 一行在 D 可见 ⟺ 它在 **D 或之前创建**,且(**没删** 或 **删于 D 之后**)。

## 2. 为什么单有创建日 `ds` 不够 —— 必须有删除日

举例:`card_x` 创建于 `day3`、删除于 `day5`。

| 查询 horizon | 期望 |
|---|---|
| as-of `day4` | **可见**(那时还没删) |
| as-of `day6` | **不可见**(已删) |

只知道创建分区 `ds=3`,判断不了「`day4` 时它是否已删」——删除发生在另一个时间点上。**删除必须也有自己的日期** `deleted_ds`。有了它,as-of 判定塌缩成一句可下推的谓词(§4)。

## 3. 四个引擎代管字段(创建 / 删除各一对:天 + 精确时刻)

| 字段 | 含义 | 是否可空 |
|---|---|---|
| `ds` | **创建日** `YYYYMMDD`(分区键) | 永不空 |
| `created_at` | 创建精确时刻(日内二级过滤 / tiebreak) | 永不空 |
| `deleted_ds` | **删除日** `YYYYMMDD` | 活行为 `NULL` |
| `deleted_at` | 删除精确时刻 | 活行为 `NULL` |

声明式 schema **不写**它们,引擎自动加(见 [api/setup.md](../api/setup.md))。对称:创建有 `(ds, created_at)`,删除有 `(deleted_ds, deleted_at)`——`_ds` 用于分区/时光机判定,`_at` 用于日内精度与审计。

## 4. 可见性判定(单行 + 一句谓词)

每个主键在 `_sb_<表>` 里**恰好一行**(主键写一次、不覆盖)。insert 落一行(`ds`/`created_at` 填、`deleted_ds`/`deleted_at` NULL);delete 对那一行 **`UPDATE deleted_ds/deleted_at`**(软删标记,行不删)。所以「as-of `D`」不需要重放事件,就是**这一行上的一句谓词**:

```sql
ds <= 'D' AND (deleted_ds IS NULL OR deleted_ds > 'D')
```

- 左半:**D 或之前创建**;右半:**没删,或删于 D 之后**。
- **当前态**(不倒带)= `deleted_ds IS NULL`(还活着的行)。
- `search()` 的向量/全文候选**共用同一条 as-of 谓词**(倒带时用上面那条,不倒带时用 `deleted_ds IS NULL`),不是只认当前态——软删的行仍留在 vss/fts 索引里,所以**回溯到它还活着的那天照样搜得到**;查和搜对齐。历史路径候选池会 over-fetch 以补回"先 top-k 后过滤"的少返回(见 [search.md](search.md) §4)。

因为主键写一次、没有值版本,时光机穿越的是**创建 / 删除两个时点**——行在它的 `ds` 出现、在它的 `deleted_ds` 消失。**不穿越「同一 key 改过的旧值」**(改值不被支持:重复主键直接报错),所以不需要多版本、不需要窗口重放,一句谓词就完备。

## 5. `ds_start` / `ds_end` 的完整语义

`ds_end` 是 **as-of horizon**(倒回那天:`ds <= ds_end` 且当时未删);`ds_start` 是额外的**创建下界**(`ds >= ds_start`)。

| 传入 | 语义 | 谓词 |
|---|---|---|
| 都不传 | 当前态 | `deleted_ds IS NULL` |
| 只 `ds_end` | **时光机**:as-of `ds_end` | `ds <= ds_end AND (deleted_ds IS NULL OR deleted_ds > ds_end)` |
| 只 `ds_start` | `ds_start` 起、至今仍活 | `deleted_ds IS NULL AND ds >= ds_start` |
| 都传 | `ds_start..ds_end` 创建、且在 `ds_end` 时仍活 | as-of `ds_end` 谓词 `AND ds >= ds_start` |

> **审计视图**(某窗口内**创建过**的行,不管后来删没删)——直接在 `sql` 里 `WHERE ds BETWEEN … AND …`;`ds_start`/`ds_end` 参数默认带 as-of(存活)语义。

## 6. 单次生命周期即全部(写一次的推论)

真值表就是 §2 的两行,落到谓词:`n1` 创建于 `day02`、删除于 `day05`。

| horizon `D` | `ds<=D` | 删除判定 | 结论 |
|---|---|---|---|
| `day01` | 否 | —— | **不可见**(还没建)✓ |
| `day03` | 是 | `deleted_ds(05) > 03` | **可见** ✓ |
| `day06` | 是 | `deleted_ds(05) ≤ 06` | **隐藏** ✓ |
| 当前 | —— | `deleted_ds` 非空 | **隐藏** ✓ |

因为主键写一次,一个 key 只有「建 → (可选)删」这一段生命周期,没有「删了又重插」的多版本——单行谓词就把它讲完。有一个白盒测试锁住这几行(`tests/time_machine`)。

## 7. 落到单表(files / DuckDB 物理表)

时间维度怎么在 [store.md](store.md) 的两层里维护(**files 纯 append;DuckDB 单表 insert 一行、delete 软删该行**):

- **insert** → 往**创建日** `files/ds=C/<表>.jsonl` append 一行快照;DuckDB `_sb_<表>` **INSERT 一行**(business + `ds`/`created_at`,`deleted_ds`/`deleted_at` NULL,含 `_vec`/`_tok`)。
- **delete** → 往**删除日** `files/ds=X/<表>.jsonl` append 一条墓碑 `{"_deleted": "<pk>", "deleted_at": "…"}`;DuckDB 对那一行 **`UPDATE deleted_ds=X, deleted_at=…`**(只动非索引元数据列,行仍在)。
- **查询**只碰这张物理表(带上面的可见性谓词),不扫文件;文件是审计 + `rebuild` 源。
- `ls files/ds=D/` = **「D 那天发生的事」**。rebuild = 按 `ds` 顺序 replay 所有 `<表>.jsonl`,put→INSERT 行、`_deleted`→对该行 `UPDATE deleted_ds`(顺序保住)。

## 8. 没有物理删:历史永久

**seekbase 不物理删、没有 vacuum。** `delete` 永远只是打 `deleted_ds` 软删标记(DuckDB 行)+ 追加一条墓碑事件(文件);被删的行**永久留着**,时光机能倒回**任意**时刻、永远不丢历史。

- **文件真·纯 append**:一次都不回改(连删除都不改旧文件)——「已写的文件永不变」这条不变式**零例外**。
- **空间单调增长**:对 memory 系统(历史即资产、churn 低、量不大)完全可接受;换来的是「历史永不丢」这条硬保证。
- 真要「彻底抹掉某行」(如 GDPR 合规硬删)——那是一个**定点**操作,将来单独加,不是常规回收。**YAGNI**。

## 9. 边界与约定

- **粒度 = 天**:同一天多个操作都落同一个 `day`;日内先后靠 `created_at` / `deleted_at`(精确时刻)tiebreak。as-of 到「当天结束」。日内时光机(精确到某时刻)不在 v1 目标内。
- **同日创删**(`C == X == T`):as-of `T` → `deleted_ds(T) ≤ T` → **隐藏**(「看 `T` 结束时的世界」,当天已删)。要看「`T` 当天曾出现过」用审计视图(§5 注)。
- **时钟**:`day` 取写入时的 **UTC 日历日**(`YYYYMMDD`);跨时区 / 单调钟细节见 DESIGN §12 待定。
- **重复删**:已软删的行再 `delete` 匹配 0(`delete` 只对当前存活的行下墓碑),不重复标记。
- **重复主键**:同一主键再 insert **直接报错**(写一次),不产生新版本。
- **未来写**:引擎按当天写入,`ds` 不接受调用方指定未来/过去日(避免污染分区语义)。

## 10. 和其他文档的关系

- [store.md](store.md):两层存储与文件布局(顶层 `ds=YYYYMMDD` 分区、每表 `<表>.jsonl`、原子性);本文补「时间维度」。
- [api/query.md](../api/query.md):`ds_start`/`ds_end` 的对外接口;§5 是它的语义定义。
- [api/setup.md](../api/setup.md):`ds` / `deleted_ds` 作为引擎代管列的声明位置。
