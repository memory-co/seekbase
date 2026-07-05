# Embedder

seekbase 核心只认一个**注入协议**,不绑定任何模型。调用方永远不见向量——只有文本经 `search()` / `searchable` 列进出。

## `Embedder` 协议

```python
class Embedder(Protocol):
    @property
    def dim(self) -> int: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...   # 可返回 awaitable
```

- `embed` **可同步可异步**:返回 coroutine 时内部会 await。
- `dim` 是向量维度,须与 schema/实例一致。
- 满足这两个成员的任意对象都能注入。

## 默认实现 `ApiEmbedder`

核心自带,`pip install seekbase` 即得(基于 `httpx`,不加载任何本地模型):

```python
from seekbase.embedders import ApiEmbedder

embedder = ApiEmbedder(
    base_url="https://api.openai.com/v1",   # OpenAI 兼容 /embeddings 端点
    api_key="sk-…",
    model="text-embedding-3-small",
    dim=1536,
    batch_size=128,       # 分批
    max_retries=3,        # 退避重试
    timeout=30.0,
)
db = await Seekbase.open(data_dir, schema=SCHEMA, embedder=embedder)
...
await embedder.aclose()   # 关 httpx 客户端
```

- 调 `POST {base_url}/embeddings`,取 `data[].embedding`;维度不符抛 `EmbedderInvalid`。
- 内部批量 + 退避重试;失败到顶抛 `EmbedderInvalid`。

> **TODO**:本地 sentence-transformers 版 embedder(`SentenceTransformerEmbedder`),同一协议、零改端口(见 DESIGN §10)。

## 两种形态

Embedder 是 **server/进程端的东西**,不是客户端的:

- **函数形态**:`Seekbase.open(..., embedder=…)` 注入。
- **HTTP 形态**:embedder 在 **server 端**——起 server 时注入给 `Seekbase.open`;`connect` 的客户端**不带 embedder**,embedding 在 server 上算(见 [server.md](server.md))。这正是「调用方不见向量、连 embedder 都不用带」在 HTTP 上的兑现。
