# time_machine — 用 `ds` 日期分区实现时光机(完备设计)

> 状态:**M4 已落**(可见性视图 + `ds_start`/`ds_end` 裁剪)。本文把「时光机」用 `ds` 分区讲完备:需要**两对**日期字段(创建 / 删除),给出可见性判定、`ds_start`/`ds_end` 的完整语义、完备性真值表、三写落地、以及「无物理删」的取舍。存储与文件布局见 [store.md](store.md)。

## 1. 目标

时光机 = 把库倒回某一天 `D`,只看见**「D 那天结束时」存在**的行。判据:

> 一行在 D 可见 ⟺ 它在 **D 或之前创建**,且(**没删** 或 **删于 D 之后**)。

## 2. 为什么单有创建日 `ds` 不够 —— 必须有删除日

举例:`card_x` 创建于 `day3`、删除于 `day5`。

| 查询 horizon | 期望 |
|---|---|
| as-of `day4` | **可见**(那时还没删) |
| as-of `day6` | **不可见**(已删) |

只知道创建分区 `ds=3`,判断不了「`day4` 时它是否已删」——删除发生在另一个时间点上。**删除必须也有自己的日期** `deleted_ds`。有了它,as-of 判定塌缩成一句可下推的谓词(§4)。这就是本次要补的字段。

## 3. 四个引擎代管字段(创建 / 删除各一对:天 + 精确时刻)

| 字段 | 含义 | 是否可空 |
|---|---|---|
| `ds` | **创建日** `YYYYMMDD`(分区键) | 永不空 |
| `created_at` | 创建精确时刻(日内二级过滤 / tiebreak) | 永不空 |
| `deleted_ds` | **删除日** `YYYYMMDD` | 活行为 `NULL` |
| `deleted_at` | 删除精确时刻 | 活行为 `NULL` |

声明式 schema **不写**它们,引擎自动加(见 [api/setup.md](../api/setup.md))。对称:创建有 `(ds, created_at)`,删除有 `(deleted_ds, deleted_at)`——`_ds` 用于分区/时光机判定,`_at` 用于日内精度与审计。

## 4. 可见性判定(事件重放,现算最新存活版本)

派生 DuckDB 是**纯 append 事件表**(每次写 = 一条 **put** 或 **del** 事件,带单调 `_seq`;**不 update、不 replace**)。「as-of `D`」= 对每个主键**重放 ≤ D 的事件、取最新那条**:

- 收集该 pk **day ≤ D** 的所有事件(put 用 `ds`、del 用 `deleted_ds`);
- 按 `_seq` 取**最新**一条:是 **put** → 该行存活,数据 = 那一版;是 **del** → 隐藏。

SQL 上就是一个窗口视图:`row_number() OVER (PARTITION BY pk ORDER BY _seq DESC)`,取 `rn = 1` 且该事件是 put(`ds IS NOT NULL`)。当前态(不倒带)= 不加 day 上界。`search()` 复用这个视图(向量结果按 pk join 进来)。

> **为什么不是「一行 + `deleted_ds` 谓词」**:那种「单行就地改 `deleted_ds`」的写法只对「建一次、删一次」的行成立;一旦**删了又重插**(seekbase 的「改」= 追加新版本),单行谓词就丢了旧版本、as-of 到重插之前会出错。**事件重放对任意 create/delete/re-insert 历史都对**(§6),代价是查询多一层窗口。

## 5. `ds_start` / `ds_end` 的完整语义

`ds_end` 是 **as-of horizon**(事件重放的截止:只看 `day ≤ ds_end` 的事件);`ds_start` 是额外的**创建下界**(过滤最终存活版本的 `ds ≥ ds_start`)。

| 传入 | 语义 | 重放 |
|---|---|---|
| 都不传 | 当前态 | 全部事件,取每 pk 最新为 put 的 |
| 只 `ds_end` | **时光机**:as-of `ds_end` | 只放 `day ≤ ds_end` 的事件 |
| 只 `ds_start` | `ds_start` 起、至今仍活 | 全部事件,存活版本再过滤 `ds ≥ ds_start` |
| 都传 | `ds_start..ds_end` 创建、且在 `ds_end` 时仍活 | `day ≤ ds_end` 重放,存活版本再滤 `ds ≥ ds_start` |

> **审计视图**(某窗口内**创建过**的版本,不管后来删没删)——直接在 `sql` 里 `WHERE ds BETWEEN … AND …`;`ds_start`/`ds_end` 参数默认带 as-of(存活)语义。

## 6. 完备性:任意 create/delete/re-insert 历史都正确

事件重放对**多版本**也成立。例:`n1` 建于 `day02`(v1)→ 删于 `day05` → 重插 `day08`(v2):

| horizon `D` | `day ≤ D` 的事件 | 最新(`_seq`) | 结论 |
|---|---|---|---|
| `day03` | put@02 | put(v1) | **可见 v1** ✓ |
| `day06` | put@02, del@05 | del | **隐藏** ✓ |
| `day09` | put@02, del@05, put@08 | put(v2) | **可见 v2** ✓ |
| 当前 | 全部 | put@08 | **可见 v2** ✓ |

「单行 + `deleted_ds` 谓词」在 `day03` 会漏掉 v1(派生表只剩 v2)——事件重放不会。单次生命周期是它的特例。有一个白盒测试锁住这四行(`tests/time_machine`)。

## 7. 落到三写(files / DuckDB / 向量)

时间维度怎么在 [store.md](store.md) 的三写里维护(**files 与 DuckDB 都是纯 append**):

- **insert 事件** → 往**创建日** `files/ds=C/<表>.jsonl` append 一行快照;DuckDB `_sb_<表>` INSERT 一条 **put** 事件(business + `ds`/`created_at` + `_seq`)。
- **delete 事件** → 往**删除日** `files/ds=X/<表>.jsonl` append 一条墓碑 `{"_deleted": "<pk>", "deleted_at": "…"}`;DuckDB INSERT 一条 **del** 事件(pk + `deleted_ds`/`deleted_at` + `_seq`,business 列 NULL、`ds` NULL)。**两边都不回改任何已写的行。**
- **查询**只碰派生表的重放视图(§4),不扫文件;文件是审计 + `rebuild` 源。
- `ls files/ds=D/` = **「D 那天发生的事」**。rebuild = 按 `ds` 顺序 replay 所有 `<表>.jsonl`,put→INSERT put 事件、`_deleted`→INSERT del 事件(`_seq` 按 replay 顺序,保住时序)。

## 8. 没有物理删:历史永久

**seekbase 不物理删、没有 vacuum。** `delete` 永远只是打 `deleted_ds` 墓碑(派生行)+ 追加一条墓碑事件(文件);被删的行**永久留着**,时光机能倒回**任意**时刻、永远不丢历史。

- **文件真·纯 append**:一次都不回改(连删除都不改旧文件)——「已写的行永不变」这条不变式**零例外**。
- **空间单调增长**:对 memory 系统(历史即资产、churn 低、量不大)完全可接受;换来的是「历史永不丢」这条硬保证。
- 真要「彻底抹掉某行」(如 GDPR 合规硬删)——那是一个**定点**操作,将来单独加,不是常规回收。**YAGNI**。

## 9. 边界与约定

- **粒度 = 天**:同一天多个事件都落同一个 `day`;日内先后靠 `_seq`(单调,= 写入顺序)定序,as-of 到「当天结束」= 取当天最后一个事件。日内时光机(精确到某时刻)不在 v1 目标内。
- **同日创删**(`C == X == T`):as-of `T` → 事件 put@T、del@T 都 ≤ T,最新是 del → **隐藏**(「看 `T` 结束时的世界」,当天已删)。要看「`T` 当天曾出现过」用审计视图(§5 注)。
- **时钟**:`day` 取写入时的 **UTC 日历日**(`YYYYMMDD`);跨时区 / 单调钟细节见 DESIGN §12 待定。
- **重复删**:已是墓碑的行再 `delete` 匹配 0(`delete` 只对当前存活的行下墓碑),不产生多余事件。
- **未来写**:引擎按当天写入,`ds` 不接受调用方指定未来/过去日(避免污染分区语义)。

## 10. 和其他文档的关系

- [store.md](store.md):三写与文件布局(顶层 `ds=YYYYMMDD` 分区、每表 `<表>.jsonl`、原子性);本文补「时间维度」的完备性。
- [api/query.md](../api/query.md):`ds_start`/`ds_end` 的对外接口;§5 是它的语义定义。
- [api/setup.md](../api/setup.md):`ds` / `deleted_ds` 作为引擎代管列的声明位置。
