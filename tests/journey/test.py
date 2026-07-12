"""journey — 一条端到端主线:本地形态(无 HTTP)把典型功能串起来走一遍.

不追求穷尽(每个功能的边界/负路径在各自目录里细测),而是像真实使用那样,
按顺序把「建库 → 混合时间批量写入 → 结构化查询 → 中文语义检索 → 时光机回溯
(含回溯已删除的历史行)→ 软删除可见性 → 重开持久化 → rebuild → 写一次性」
连成一个故事,验证它们协作无碍。See README.md.

时间维度:公共写入永远盖「今天」的 ds。为了让时光机真的能跨天回溯,这里
monkeypatch 写入路径的时钟(``write_service.today/now``)把前两批落到历史 ds,
第三批 ``undo`` 回真实当前时间——写入仍全程走 ``db.insert``/``db.delete`` 公共路径。
"""
from __future__ import annotations

import pytest

import seekbase.service.write_service as _ws
from seekbase import QueryError
from tests.conftest import open_db

# ─── 数据集:30 条中文技术笔记,6 主题各 5 条(title/body 均可搜索)──────
_RAW = [
    # cache 缓存
    ("n01", "cache", "Redis 淘汰策略", "Redis 缓存淘汰策略 LRU 与 LFU 的对比分析"),
    ("n02", "cache", "本地与远端缓存", "本地缓存与远端 Redis 缓存的一致性取舍"),
    ("n03", "cache", "缓存三大问题", "缓存穿透 缓存击穿 缓存雪崩 三大问题的防护"),   # ← day04 删除
    ("n04", "cache", "多级缓存预热", "多级缓存架构中的热点数据探测与预热"),
    ("n05", "cache", "TTL 与惊群", "缓存过期时间 TTL 设置与缓存惊群效应的规避"),
    # terminal 终端
    ("n06", "terminal", "pty 与 tmux", "为什么伪终端 pty 会让人联想到 tmux 终端复用器"),
    ("n07", "terminal", "tmux 对比 screen", "tmux 与 screen 两大终端复用器的会话管理对比"),
    ("n08", "terminal", "断线恢复会话", "SSH 远程登录终端断线后如何恢复会话"),
    ("n09", "terminal", "ANSI 转义", "终端颜色与 ANSI 转义序列的渲染原理"),
    ("n10", "terminal", "作业控制", "shell 作业控制与前台后台进程的信号处理"),
    # vector 向量检索
    ("n11", "vector", "嵌入与近邻", "机器学习里的向量嵌入与近邻相似度检索"),
    ("n12", "vector", "HNSW 索引", "HNSW 图索引在高维向量近邻检索中的应用"),
    ("n13", "vector", "相似度度量", "余弦相似度与欧氏距离在向量检索里的差异"),
    ("n14", "vector", "召回与延迟", "向量数据库的召回率与查询延迟的权衡"),
    ("n15", "vector", "稠密向量", "文本嵌入模型把句子映射到稠密向量空间"),
    # storage 存储
    ("n16", "storage", "单文件 DuckDB", "单文件 DuckDB 同时承担结构化查询与向量检索"),
    ("n17", "storage", "列存与行存", "列式存储与行式存储在分析查询上的性能差异"),
    ("n18", "storage", "LSM 与 B 树", "LSM 树与 B 树两种存储引擎的写放大对比"),
    ("n19", "storage", "预写日志", "预写日志 WAL 如何保证数据库崩溃一致性"),
    ("n20", "storage", "分区裁剪", "分区表按日期裁剪扫描以加速时间范围查询"),
    # fts 全文检索
    ("n21", "fts", "BM25 与分词", "BM25 打分与 jieba 中文分词构成全文检索"),
    ("n22", "fts", "倒排索引", "倒排索引的构建与查询词的布尔匹配"),
    ("n23", "fts", "分词歧义", "中文分词的歧义切分与未登录词识别难题"),
    ("n24", "fts", "TF-IDF", "TF-IDF 权重在关键词检索中的作用"),
    ("n25", "fts", "混合检索融合", "混合检索把向量语义与 BM25 关键词用 RRF 融合"),
    # ml 机器学习
    ("n26", "ml", "反向传播", "梯度下降与反向传播训练神经网络的原理"),
    ("n27", "ml", "正则化", "过拟合与正则化 dropout 提升模型泛化能力"),
    ("n28", "ml", "注意力机制", "注意力机制与 Transformer 架构的序列建模"),
    ("n29", "ml", "数据集划分", "交叉验证与训练集验证集测试集的划分"),
    ("n30", "ml", "学习率调度", "学习率调度与 warmup 预热对模型收敛的影响"),
]
NOTES = [{"id": i, "topic": t, "title": ti, "body": b, "weight": k + 1}
         for k, (i, t, ti, b) in enumerate(_RAW)]

SCHEMA = [{
    "table": "notes",
    "columns": [
        {"name": "id", "type": "str"},
        {"name": "title", "type": "str"},
        {"name": "body", "type": "str"},
        {"name": "topic", "type": "str"},
        {"name": "weight", "type": "int"},
    ],
    "primary": "id",
    "searchable": ["title", "body"],
}]


def _freeze(monkeypatch, ds: str) -> None:
    """把写入路径的时钟钉在某个历史 ds(create/delete/ticket 都用它)。"""
    monkeypatch.setattr(_ws, "today", lambda: ds)
    monkeypatch.setattr(_ws, "now", lambda: ds + "T12:00:00+00:00")


def _ids(rows) -> list[str]:
    return [r["id"] for r in rows]


async def test_full_local_journey(tmp_path, monkeypatch):
    db = await open_db(tmp_path, schema=SCHEMA)

    # ── 1. 历史批 A(ds=20260101):写入前 12 条 ────────────────────────
    _freeze(monkeypatch, "20260101")
    await db.wait(await db.insert("notes", NOTES[0:12]))

    # ── 2. 历史批 B(ds=20260104):再写 12 条,并删掉批 A 的 n03 ────────
    _freeze(monkeypatch, "20260104")
    await db.wait(await db.insert("notes", NOTES[12:24]))
    await db.wait(await db.delete("notes", where="id = ?", params=["n03"]))

    # ── 3. 当前时间批 C:恢复真实时钟,写入最后 6 条(“今天”的数据)───
    monkeypatch.undo()
    await db.wait(await db.insert("notes", NOTES[24:30]))

    try:
        # ── 4. 结构化查询:计数 / 过滤 / 排序限量 / 参数化 IN ──────────
        (total,) = await db.query("SELECT count(*) AS c FROM notes")
        assert total["c"] == 29                       # 30 写入 - 1 删除(n03)

        cache_live = await db.query("SELECT id FROM notes WHERE topic = 'cache' ORDER BY id")
        assert _ids(cache_live) == ["n01", "n02", "n04", "n05"]   # n03 已删

        top2 = await db.query("SELECT id FROM notes ORDER BY weight DESC LIMIT 2")
        assert _ids(top2) == ["n30", "n29"]

        (picked,) = await db.query(
            "SELECT count(*) AS c FROM notes WHERE id IN (?, ?)", params=["n01", "n11"])
        assert picked["c"] == 2

        # ── 5. 中文语义检索:search() 把想要的那条捞出来 ────────────────
        hits = await db.query(
            "SELECT id, _score FROM notes WHERE search(body, '缓存淘汰策略') ORDER BY _score DESC")
        assert hits[0]["id"] == "n01"                 # BM25(jieba)精确命中 → 排第一
        assert all(h["_score"] is not None for h in hits)

        term = await db.query(
            "SELECT id FROM notes WHERE search(body, '终端复用器') ORDER BY _score DESC LIMIT 3")
        assert _ids(term)[0] in {"n06", "n07"}        # 终端复用器的两条之一居首

        vec = await db.query(
            "SELECT id FROM notes WHERE search(body, '向量近邻检索') ORDER BY _score DESC LIMIT 3")
        assert _ids(vec)[0] in {"n11", "n12"}

        # 检索 + 结构化过滤同一句
        fts = await db.query(
            "SELECT id FROM notes WHERE search(body, '中文分词') AND topic = 'fts'")
        assert fts and "n21" in _ids(fts)

        # ── 6. 时光机:按历史 ds 回溯,看到当时的世界 ───────────────────
        async def count_asof(ds_end=None, ds_start=None):
            (r,) = await db.query(
                "SELECT count(*) AS c FROM notes", ds_start=ds_start, ds_end=ds_end)
            return r["c"]

        assert await count_asof(ds_end="20260101") == 12    # 只有批 A,n03 尚在
        assert await count_asof(ds_end="20260103") == 12    # 批 B 还没写(在 04)
        assert await count_asof(ds_end="20260105") == 23    # A + B - n03(04 删)
        assert await count_asof(ds_end="20990101") == 29    # 含“今天”那批
        assert await count_asof(ds_start="20990101") == 0    # 未来起点 → 空

        # 回溯已删除的历史行:n03 于 20260104 被删。search() 尊重 ds 窗口,和结构化
        # 查询共用同一可见性谓词——回到它还活着的 day03 能搜到,as-of now 搜不到。
        past = await db.query(
            "SELECT id FROM notes WHERE search(body, '缓存穿透') ORDER BY _score DESC",
            ds_end="20260103")
        assert "n03" in _ids(past)                     # day03:历史里它还活着,可检索
        now_hit = await db.query(
            "SELECT id FROM notes WHERE search(body, '缓存穿透') ORDER BY _score DESC")
        assert "n03" not in _ids(now_hit)              # 现在已软删,search 检索不到

        # ── 7. 重开:数据落盘,持久化跨进程 ─────────────────────────────
        await db.close()
        db = await open_db(tmp_path, schema=SCHEMA)
        (again,) = await db.query("SELECT count(*) AS c FROM notes")
        assert again["c"] == 29
        reopened = await db.query(
            "SELECT id FROM notes WHERE search(body, '缓存淘汰策略') ORDER BY _score DESC LIMIT 1")
        assert reopened[0]["id"] == "n01"              # 重开后 search 仍工作

        # ── 8. rebuild:清空派生索引、重放文件镜像,ds 保真、search 复活 ──
        await db.wait(await db.rebuild())
        (rebuilt,) = await db.query("SELECT count(*) AS c FROM notes")
        assert rebuilt["c"] == 29
        assert await count_asof(ds_end="20260101") == 12   # 历史 ds 经镜像重放不丢
        revived = await db.query(
            "SELECT id FROM notes WHERE search(body, '向量近邻检索') ORDER BY _score DESC LIMIT 3")
        assert _ids(revived)[0] in {"n11", "n12"}

        # ── 9. 写一次性:主键写死,重插既有 id 被拒 ─────────────────────
        with pytest.raises(QueryError):
            await db.insert("notes", {"id": "n01", "title": "x", "body": "y",
                                      "topic": "cache", "weight": 99})
    finally:
        await db.close()
