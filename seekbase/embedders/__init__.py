"""Embedder implementations. The core only knows the ``Embedder`` protocol
(see ``seekbase._types``); implementations plug in through it.

v1 ships ``ApiEmbedder`` as the default (httpx is a core dep, so it works out
of the box). A local sentence-transformers embedder is a TODO (DESIGN §10).
"""
from __future__ import annotations

from .api import ApiEmbedder

__all__ = ["ApiEmbedder"]
