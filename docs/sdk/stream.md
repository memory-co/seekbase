# 流式摄取:`db.stream`(嵌入专属)

常驻的无界管道([works/pipeline-streaming.md](../works/pipeline-streaming.md)):**流只摄取,查询永远是 landed 表上的有界 SQL**。

```python
handle = await db.stream(
    pipeline,             # "watch '<glob>' [| bash 中段…] | ingest <表>"
    *,
    name,                 # str:流名(checkpoint 按它存;同名运行中不许重复启动)
) -> StreamHandle
```

```python
h = await db.stream(
    "watch 'logs/**/*.jsonl' "
    "| jq 'select(.level==\"error\") | {card_id:.id, issue:.msg, kind:.level, n:1}' "
    "| ingest cards --flush-ms 200",
    name="error-ingest")
...
await h.stop()            # 优雅停:补吃最后一批、提交 checkpoint
```

## 管道形状(编译期校验)

```
watch '<glob>' [--poll-ms 200]        源:必须无界;跟文件新增的整行
  | <bash 中段…>                       可选:sh / jq / 自定义 optimize_bash 算子(EXEC → 要 sandboxed 策略)
  | ingest <表> [--batch 64] [--flush-ms 200]    尾:必须是 ingest
```

- 流里**不放 SQL 段**(分析去 `db.query`);源必须无界、尾必须 `ingest`,否则 `QueryError`。
- `watch` 进 `db.query`(有界)同样编译期拒——无界流进不了 duck runtime。
- 行必须是 JSON 对象、字段对上表列(多余列 `QueryError`,缺的填 NULL)——用 `jq` 中段整形。

## 交付语义:at-least-once + 幂等 sink

- **checkpoint = per-file 字节 offset**,**落库之后才提交**(崩在中间 → 重放该批);只消费**完整行**(半行等补全)。
- **幂等**:ingest 按主键去重(`skip_existing`)——重放的行被跳过,**事实上恰好一次**。
- 重启:同名流从 checkpoint 续读;停机期间追加的行补上。
- `searchable` 列照常在摄取时 embed——落库即可搜;这也是流吞吐的主要成本。

## `StreamHandle`

```python
h.name              # str
h.running           # bool
await h.stop()      # 停止(幂等);drain + final checkpoint
h.exception()       # 流若因错终止 → 异常对象;运行中/正常停 → None
```

`db.close()` 会自动停掉所有在跑的流。

## 错误

| 情况 | 异常 |
|---|---|
| 远程形态调用 `db.stream` | `QueryError`(嵌入专属) |
| 形状不合法(源有界 / 尾不是 ingest / 含 SQL 段 / 中段非 bash-native) | `QueryError` |
| 中段是 EXEC 算子但策略未升级 | `PermissionDenied` |
| 同名流已在跑 | `QueryError` |
| 未知表(ingest 目标) | `SchemaError` |
