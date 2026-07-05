"""VectorEngine — the vector/semantic side (LanceDB).

One LanceDB table per searchable seekbase table, storing ``(pk, vector)``. The
injected embedder turns text into vectors; the caller never sees vectors. All
LanceDB calls are sync, run off the event loop via ``asyncio.to_thread``; the
embedder may be sync or async.

This is the M3 landing of searchbase's local backend, pared down to what
seekbase needs (upsert / delete by id, ANN search).
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
        self._tables = tables            # seekbase table name -> lance table

    @classmethod
    async def create(cls, lance_dir, embedder, schema) -> "VectorEngine":
        import lancedb
        import pyarrow as pa

        dim = int(embedder.dim)
        db = await asyncio.to_thread(lancedb.connect, str(lance_dir))
        tables = {}
        for spec in schema.tables:
            if not spec.searchable:
                continue
            pa_schema = pa.schema([
                pa.field("pk", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), dim)),
            ])
            tables[spec.name] = await asyncio.to_thread(
                lambda s=pa_schema, n=spec.name: db.create_table(
                    f"vec_{n}", schema=s, exist_ok=True
                )
            )
        return cls(db, embedder, dim, tables)

    async def _embed_one(self, text: str) -> list[float]:
        r = self._embedder.embed([text])
        if inspect.isawaitable(r):
            r = await r
        vec = [float(x) for x in r[0]]
        if len(vec) != self._dim:
            raise EmbedderInvalid(f"expected dim {self._dim}, got {len(vec)}")
        return vec

    async def upsert(self, table: str, pk, text: str) -> None:
        if table not in self._tables:
            return
        vec = await self._embed_one(text)
        t = self._tables[table]

        def _do():
            t.delete(f"pk = {_q(pk)}")
            t.add([{"pk": str(pk), "vector": vec}])

        await asyncio.to_thread(_do)

    async def delete(self, table: str, pk) -> None:
        if table not in self._tables:
            return
        t = self._tables[table]
        await asyncio.to_thread(lambda: t.delete(f"pk = {_q(pk)}"))

    async def search(self, table: str, text: str, k: int) -> list[tuple[str, float]]:
        if table not in self._tables:
            return []
        vec = await self._embed_one(text)
        t = self._tables[table]

        def _do():
            try:
                rows = t.search(vec).metric("cosine").limit(k).to_list()
            except Exception:
                return []
            # cosine distance in [0,2]; score = 1 - distance (higher = closer)
            return [(str(r["pk"]), 1.0 - float(r.get("_distance", 0.0))) for r in rows]

        return await asyncio.to_thread(_do)

    async def close(self) -> None:
        return None
