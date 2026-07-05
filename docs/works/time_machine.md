# time_machine — 用 `ds` 日期分区实现时光机(完备设计)

> 状态:设计(落地 M4)。本文把「时光机」这件事用 `ds` 分区讲完备:需要**两对**日期字段(创建 / 删除),给出可见性判定、`ds_start`/`ds_end` 的完整语义、完备性真值表、三写落地、以及 `vacuum` 的正确语义。存储与文件布局见 [store.md](store.md)。

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

## 4. 可见性判定(核心谓词)

「as-of `D`」= 一句谓词:

```sql
ds <= D  AND  (deleted_ds IS NULL OR deleted_ds > D)
```

- `ds <= D` —— **分区裁剪**,只扫 ≤ D 的创建分区(扫描量随时间窗收敛)。
- `deleted_ds IS NULL OR deleted_ds > D` —— 活着,或删在 D 之后。

当前态(不倒带)就是 `deleted_ds IS NULL`。`search()` 复用同一谓词(向量侧按 `ds` / `deleted_ds` pre-filter)。

## 5. `ds_start` / `ds_end` 的完整语义

`ds_end` 一身二职:**创建上界** + **删除 horizon**(你「站在」的那天);`ds_start` 是额外的**创建下界**。

| 传入 | 语义 | 谓词 |
|---|---|---|
| 都不传 | 当前态 | `deleted_ds IS NULL` |
| 只 `ds_end` | **时光机**:as-of `ds_end` | `ds <= ds_end AND (deleted_ds IS NULL OR deleted_ds > ds_end)` |
| 只 `ds_start` | `ds_start` 起、至今仍活 | `ds >= ds_start AND deleted_ds IS NULL` |
| 都传 | `ds_start..ds_end` 创建、且在 `ds_end` 时仍活 | `ds BETWEEN ds_start AND ds_end AND (deleted_ds IS NULL OR deleted_ds > ds_end)` |

> **审计视图**(某窗口内**创建过**的行,不管后来删没删)不套删除 horizon——直接在 `sql` 里写 `WHERE ds BETWEEN … AND …`。`ds_start`/`ds_end` 参数默认带 as-of(存活)语义;要纯创建过滤就用 SQL 列。

## 6. 完备性:所有时序组合都正确

设某行创建于 `C`、删除于 `X`(或未删),查询 horizon `D`。逐情形验证 §4 谓词:

| 情形 | 期望可见? | `ds<=D` | `deleted_ds NULL/>D` | 结论 |
|---|---|---|---|---|
| `C ≤ D`,未删 | 是 | ✓ | `NULL` ✓ | **可见** ✓ |
| `C ≤ D`,删于 `X > D` | 是 | ✓ | `X > D` ✓ | **可见** ✓ |
| `C ≤ D`,删于 `X ≤ D` | 否 | ✓ | `X ≤ D` ✗ | **隐藏** ✓ |
| `C > D` | 否 | ✗(分区裁掉) | — | **隐藏** ✓ |

四种时序全覆盖 → 逻辑完备。

## 7. 落到三写(files / DuckDB / 向量)

时间维度怎么在 [store.md](store.md) 的三写里维护(文件 = 按天分区、每表一个 append 日志 `ds=D/<表>.jsonl`):

- **insert 事件** → 往**创建日** `files/ds=C/<表>.jsonl` append 一行(行快照,`deleted_ds` 初始空)。
- **delete 事件** → 往**删除日** `files/ds=X/<表>.jsonl` append 一条墓碑 `{"_deleted": "<pk>", "deleted_at": "…"}`——**不回改任何已写的行**,纯 append。
- **派生 DuckDB 行**:一行带 `ds / created_at / deleted_ds / deleted_at`;消费 delete 事件时按主键把该行的 `deleted_ds`/`deleted_at` 置上(派生表可就地改,canonical 文件不改)。
- **查询**只碰派生表(§4 谓词),不扫文件;文件是审计 + `rebuild` 源。
- `ls files/ds=D/` = **「D 那天发生的事」** = 当天写的行 + 当天删除的墓碑。rebuild = 按 `ds` 顺序 replay 所有 `<表>.jsonl`(insert 建行 / latest-wins、`_deleted` 按主键置 `deleted_ds`)。

## 8. `vacuum`:按行清死行,**不是**整块删分区

`vacuum(before=D)` = 「我不再需要倒带到 `D` 之前」。此时可以物理删掉**在每个 `≥ D` 的 horizon 都不可见**的行,即:

```
deleted_ds < D          -- 删除发生在 D 之前 → 任何 ≥D 的视角都看不到它 → 可丢
```

- **活行,以及删于 `≥ D` 的行,一律保留**——它们在 horizon `≥ D` 仍可见。
- 所以 `vacuum` **不能**简单 `rm -rf ds<D/`:那会误删「**早创建但仍活**」的行(它的 insert 行在旧分区的 jsonl 里)。正确做法是按 `deleted_ds < D` **逐行**清理:**重写受影响的 `<表>.jsonl`**、丢掉这些行的 insert / 墓碑事件(整表整天全死就删掉该文件),同步清派生行 + 向量。
- 代价明确:`vacuum(before=D)` 后只能倒带到 `≥ D`;`D` 之前的删除历史被丢弃。

## 9. 边界与约定

- **粒度 = 天**:同一天多次创建/删除都落同一个 `ds`;日内先后看 `created_at`/`deleted_at`。日内时光机(精确到某时刻)不在 v1 目标内。
- **同日创删**(`C == X == T`):as-of `T` → `deleted_ds = T` 不 `> T` → **隐藏**(语义是「看 `T` 结束时的世界」,当天已删故不可见)。要看「`T` 当天曾出现过」用审计视图(§5 注)。
- **时钟**:`ds` 取写入时的 **UTC 日历日**(`YYYYMMDD`);跨时区 / 单调钟的细节见 DESIGN §12 待定。
- **重复删**:已是墓碑的行再 `delete` 无效(不覆盖 `deleted_ds`,保留首次删除日)。
- **未来写**:引擎按当天写入,`ds` 不接受调用方指定未来/过去日(避免污染分区语义)。

## 10. 和其他文档的关系

- [store.md](store.md):三写与文件布局(顶层 `ds=YYYYMMDD` 分区、每表 `<表>.jsonl`、原子性);本文补「时间维度」的完备性。
- [api/query.md](../api/query.md):`ds_start`/`ds_end` 的对外接口;§5 是它的语义定义。
- [api/setup.md](../api/setup.md):`ds` / `deleted_ds` 作为引擎代管列的声明位置。
