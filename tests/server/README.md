# server — server 形态:同一条链走 HTTP

## 这个场景在测什么

两形态共用一个端口 —— 把嵌入形态那条链原封不动搬到 HTTP 上,行为一致:

1. **全链路 round-trip**:client 的 `insert` / `select` / `count` 经
   `POST /v1/execute` 打到 server,结果与直接在嵌入 server_db 上查一致。
2. **错误保型过线**:server 侧抛的异常按类型映射 HTTP 状态码,client 侧重建同
   类型 —— `QueryError` / `ReadOnlyError` / `NotSupportedYet` 都还原成原类。
3. **`as_of` 只读在 HTTP 上也生效**:client 带 `as_of` 连接 → 回退可见性 + 写被
   `ReadOnlyError` 挡(权威判定在 server 的 `LocalExecutor`)。
4. **鉴权**:server 配了 bearer token,错 token 的 client 被拒。
5. **health**:`GET /v1/health` 返回 `{"ready": true}`。
6. **runner 外部注入**:`serve()` 调用你传入的任意 `runner(app, host=, port=)`,
   证明跑 app 不需要把 uvicorn 作为依赖。

## 不在这测什么

- 结构化 / 墓碑 / 时光机的**语义本身**在各自嵌入场景里已锁,这里只验「过 HTTP
  不走样」,不重复断言语义细节。
- 真起端口 / uvicorn —— 用 httpx 的 in-process `ASGITransport`,不开 socket。

## fixture 来源

- `pair`(`tests/conftest.py`)—— `(server_db, client)`,in-process ASGI,自动关
- `open_db` / `client_for`(`tests/conftest.py`)—— as-of / 鉴权变体自己拼
- `seekbase.server.serve` —— runner 注入用例直接调
