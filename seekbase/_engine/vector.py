"""VectorEngine — the vector/semantic side (LanceDB).

One LanceDB table **per (seekbase table, searchable column)**, storing
``(pk, vector)``. Each searchable column gets its own index, so
``search(column, 'text')`` searches exactly that column. The injected embedder
turns text into vectors; the caller never sees vectors. LanceDB calls run off
the event loop via ``asyncio.to_thread``; the embedder may be sync or async.
"""
from __future__ import annotations

import asyncio
import inspect

from .._types import EmbedderInvalid


def _q(pk) -> str:
    return "'" + str(pk).replace("'", "''") + "'"


class VectorEngine:
    def __init__(self, db, embedder, dim: int, tables: dict) -> None:
        self._db = db
        self._embedder = embedder
        self._dim = dim
        self._tables = tables            # (table, column) -> lance table

    @classmethod
    async def create(cls, lance_dir, embedder, schema) -> "VectorEngine":
        import lancedb
        import pyarrow as pa

        dim = int(embedder.dim)
        db = await asyncio.to_thread(lancedb.connect, str(lance_dir))
        tables = {}
        for spec in schema.tables:
            for col in spec.searchable:
                pa_schema = pa.schema([
                    pa.field("pk", pa.string()),
                    pa.field("vector", pa.list_(pa.float32(), dim)),
                ])
                tables[(spec.name, col)] = await asyncio.to_thread(
                    lambda s=pa_schema, n=spec.name, c=col: db.create_table(
                        f"vec_{n}__{c}", schema=s, exist_ok=True))
        return cls(db, embedder, dim, tables)

    async def _embed_one(self, text: str) -> list[float]:
        r = self._embedder.embed([text])
        if inspect.isawaitable(r):
            r = await r
        vec = [float(x) for x in r[0]]
        if len(vec) != self._dim:
            raise EmbedderInvalid(f"expected dim {self._dim}, got {len(vec)}")
        return vec

    async def upsert(self, table: str, col: str, pk, text: str) -> None:
        t = self._tables.get((table, col))
        if t is None:
            return
        vec = await self._embed_one(text)

        def _do():
            t.delete(f"pk = {_q(pk)}")
            t.add([{"pk": str(pk), "vector": vec}])

        await asyncio.to_thread(_do)

    async def delete(self, table: str, col: str, pk) -> None:
        t = self._tables.get((table, col))
        if t is None:
            return
        await asyncio.to_thread(lambda: t.delete(f"pk = {_q(pk)}"))

    async def search(self, table: str, col: str, text: str, k: int) -> list[tuple[str, float]]:
        t = self._tables.get((table, col))
        if t is None:
            return []
        vec = await self._embed_one(text)

        def _do():
            try:
                rows = t.search(vec).metric("cosine").limit(k).to_list()
            except Exception:
                return []
            return [(str(r["pk"]), 1.0 - float(r.get("_distance", 0.0))) for r in rows]

        return await asyncio.to_thread(_do)

    async def close(self) -> None:
        return None
