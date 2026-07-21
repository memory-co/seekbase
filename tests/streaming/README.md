# streaming — 常驻无界管道(watch | … | ingest)

## 这个场景在测什么

`db.stream("watch '<glob>' | [bash 中段…] | ingest <表>")` 起一条**常驻流**
(pipeline-streaming.md):watch 跟文件新增行、微批经中段整形、ingest 走正常
写路径落库。**流只摄取;查询永远是 landed 表上的有界 SQL。**

1. **落库 + 幂等**:新行落库;重放已存在 pk 的行被**跳过**(at-least-once +
   幂等 sink = 事实上恰好一次;`insert(skip_existing=True)`)。
2. **落库即可搜**:流进来的行照常 embed,`search` 立即可命中。
3. **checkpoint 重启**:per-file 字节 offset **落库后才提交**;同名流重启不
   重灌,停机期间追加的行补上。
4. **半行等待**:没有换行符的尾行不消费,补全后才落。
5. **有界性守卫**:`watch` 进有界 `query` → 编译期拒;`ingest` 进 query 同理。
6. **形状校验**:流必须「无界源开头、ingest 收尾、中段全 bash-native、无 SQL 段」。
7. **中段整形**(jq,skipif 未装):EXEC 中段要 `sandboxed` 策略;每微批一条
   进程链(batch-scoped,EOF 驱动)——这是拿到「落库后才提交 offset」精确性
   的代价,常驻链是后续优化(见 stream_service.py 模块注释)。

## 不在这测什么

- 检索质量走 [`search/`](../search/);策略判定细节走 [`policy/`](../policy/)

## fixture 来源

- `db`(conftest)+ `tmp_path` 下的 jsonl 日志文件
