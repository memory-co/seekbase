# Embedder:注入协议 + 内置 `ApiEmbedder`

seekbase 核心**不捆绑任何模型**——schema 声明了 `searchable` 列时,你注入一个满足 `Embedder` 协议的对象;调用方永远不碰向量(写入时自动 embed 落索引、`search` 段自动 embed 查询文本)。

## `Embedder` 协议

```python
from seekbase import Embedder      # typing.Protocol,runtime_checkable

class MyEmbedder:
    dim: int                                   # 向量维度(属性)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量 embed。同步返回或返回 awaitable 都行(内部会 await)。"""
```

- `dim` 在 `open` 时读取一次,决定向量列/数据集的维度;之后换维度 = 换库(需 rebuild)。
- `embed` 会被批量调用(写入一批行 / 一次查询文本);返回顺序必须与输入对齐。

## 内置:`ApiEmbedder`(OpenAI 兼容 API)

```python
from seekbase.embedders import ApiEmbedder

emb = ApiEmbedder(
    base_url="https://api.siliconflow.cn/v1",   # POST {base_url}/embeddings
    api_key="sk-…",
    model="BAAI/bge-m3",
    dim=1024,                # 必须与模型输出一致
    batch_size=128,          # 每次请求最多多少条
    max_retries=3,           # 瞬时失败重试(退避)
    timeout=30.0,
)
db = await Seekbase.open("./data", schema=SCHEMA, embedder=emb)
```

httpx 是核心依赖,开箱即用;本地 sentence-transformers 形态是 TODO(自己包一个满足协议即可)。

## 测试用:确定性假 embedder

不想联网时(tests/conftest.py 的做法):bag-of-chars 之类**确定性**向量——相同文本同向量、不同文本不同方向,排序有真实信号、零依赖。中文关键词命中由 jieba+BM25 保证,不依赖 embedder 语义质量。

## 错误

| 情况 | 异常 |
|---|---|
| schema 有 searchable 列但 `embedder=None` | `EmbedderInvalid`(open 时) |
| embed 返回维度 ≠ `dim` / API 持续失败 | `EmbedderInvalid` / 原始网络异常上浮(写入失败,整批拒) |
