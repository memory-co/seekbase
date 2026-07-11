# embedder_live — 真实 embedding API 的端到端验证(需环境变量)

## 这个场景在测什么

用**真实的 `ApiEmbedder`**(OpenAI 兼容 `/embeddings` 端点)跑一遍 insert →
insert 内联 embed → DuckDB `vss`+`fts`(就地在业务表)→ `search()` hybrid 排序,验证真 embedding 下
语义命中正确。**默认 skip**:只有设了环境变量才跑,所以不影响 CI / 没 key 的人。

## 怎么跑

配置全在**环境变量**里(key 绝不进代码):

```bash
export QWEN_KEY=...                    # API key(必填,缺了就 skip)
export SEEKBASE_EMBED_URL=https://dashscope.aliyuncs.com/compatible-mode/v1   # base_url(必填)
export SEEKBASE_EMBED_MODEL=text-embedding-v4    # 可选,默认 text-embedding-v4
export SEEKBASE_EMBED_DIM=1024                    # 可选,默认 1024

.venv/bin/python -m pytest tests/embedder_live -q
```

`ApiEmbedder` 会请求 `{SEEKBASE_EMBED_URL}/embeddings`;key 从 `QWEN_KEY` 读。

## 不在这测什么

- 语义 `search()` 的逻辑(排序 / 组合 / 删除)已在 [`search/`](../search/) 用确定性
  `FakeEmbedder` 覆盖;本场景只多验「真 embedder 接得通、维度对、语义命中」。

## fixture 来源

- 无 —— 直接构造 `ApiEmbedder`(从 env)+ `Seekbase.open`
