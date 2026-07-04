"""ApiEmbedder — the default embedder (DESIGN §4.6).

Calls an OpenAI-compatible ``POST {base_url}/embeddings`` endpoint. Async
(httpx), batches inputs, and retries transient failures with backoff. httpx is
a core dependency, so this works out of the box with ``pip install seekbase``.
"""
from __future__ import annotations

import asyncio

import httpx

from .._types import EmbedderInvalid


class ApiEmbedder:
    """Satisfies the ``Embedder`` protocol against an OpenAI-compatible API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dim: int,
        batch_size: int = 128,
        max_retries: int = 3,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dim = dim
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            out.extend(await self._embed_batch(batch))
        return out

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        url = f"{self._base_url}/embeddings"
        payload = {"model": self._model, "input": batch}
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await self._client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()["data"]
                vectors = [item["embedding"] for item in data]
                for v in vectors:
                    if len(v) != self._dim:
                        raise EmbedderInvalid(
                            f"expected dim {self._dim}, got {len(v)} from {self._model}"
                        )
                return vectors
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                await asyncio.sleep(2**attempt * 0.2)  # 0.2s, 0.4s, 0.8s
        raise EmbedderInvalid(f"embedding API failed after {self._max_retries} tries: {last_exc}")

    async def aclose(self) -> None:
        await self._client.aclose()
