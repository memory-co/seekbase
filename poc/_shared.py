"""POC 共享件:中文语料、embedder、CJK 分词。

目标:两种方案都要能「在 SQL 里直接模糊检索中文」。这里统一提供
- CORPUS / QUERIES:一份带明显语义簇的中文语料 + 查询
- get_embedder():有 QWEN_KEY 环境变量就用真·中文向量(text-embedding-v3,
  dim 1024),否则退回确定性 hash embedder(仅验证机械链路,无真语义)
- cjk_tokenize():把中文切成 bigram(FTS/BM25 对中文的最小可用分词,
  无依赖;生产上换 jieba)

env:
  QWEN_KEY   —— 阿里云 DashScope key(可选;不设则用 hash embedder)
"""
from __future__ import annotations

import hashlib
import math
import os
import re

# ── 中文语料:三个语义簇(终端 / 缓存 / 向量检索) ──────────────────
CORPUS = [
    ("d1", "为什么伪终端 pty 会让人联想到 tmux 终端复用器"),
    ("d2", "tmux 会话管理与窗格分割的快捷键"),
    ("d3", "Redis 缓存淘汰策略 LRU 与 LFU 的对比"),
    ("d4", "缓存穿透和缓存雪崩的常见解决方案"),
    ("d5", "机器学习里的向量嵌入与近邻相似度检索"),
    ("d6", "用余弦相似度做语义搜索的原理"),
]

# (查询文本, 期望命中的 doc 前缀, 说明)
QUERIES = [
    ("终端多路复用工具", {"d1", "d2"}, "语义:终端复用 ≈ tmux/pty"),
    ("缓存过期与失效", {"d3", "d4"}, "语义:过期/失效 ≈ 淘汰/穿透"),
    ("语义向量搜索", {"d5", "d6"}, "语义:向量/余弦相似度"),
]

# FTS 关键词查询(词法命中,验证中文分词后 BM25 能不能中)
FTS_QUERIES = [
    ("缓存", {"d3", "d4"}),
    ("tmux", {"d1", "d2"}),
    ("相似度", {"d5", "d6"}),
]


# ── CJK 分词:ASCII 词原样,中文切 bigram ─────────────────────────
def cjk_tokenize(text: str) -> list[str]:
    out: list[str] = []
    for run in re.findall(r"[A-Za-z0-9]+|[一-鿿]+", text):
        if run[0].isascii():
            out.append(run.lower())
        elif len(run) == 1:
            out.append(run)
        else:
            out += [run[i : i + 2] for i in range(len(run) - 1)]
    return out


def cjk_tokens_str(text: str) -> str:
    """空格分隔的 token 串——喂给 FTS(它按空白切词)。"""
    return " ".join(cjk_tokenize(text))


# ── embedder ─────────────────────────────────────────────────────
class HashEmbedder:
    """确定性 hash embedder:CJK bigram → 稀疏计数向量 → L2 归一。
    只验证机械链路(能建索引、能按相似度排序),不代表真语义质量。"""

    dim = 256

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in cjk_tokenize(t):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                v[h % self.dim] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            vecs.append([x / n for x in v])
        return vecs


class QwenEmbedder:
    """真·中文向量:DashScope OpenAI 兼容 /embeddings,text-embedding-v3。"""

    dim = 1024

    def __init__(self, key: str) -> None:
        import httpx

        self._c = httpx.Client(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            headers={"Authorization": f"Bearer {key}"},
            timeout=30.0,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        r = self._c.post("/embeddings", json={"model": "text-embedding-v3", "input": texts})
        r.raise_for_status()
        return [d["embedding"] for d in r.json()["data"]]


def get_embedder():
    key = os.environ.get("QWEN_KEY")
    if key:
        print("  embedder: QwenEmbedder (真·中文向量, dim=1024)")
        return QwenEmbedder(key)
    print("  embedder: HashEmbedder (机械链路, dim=256; 设 QWEN_KEY 用真向量)")
    return HashEmbedder()
