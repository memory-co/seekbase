"""Embedder implementations. The core only knows the ``Embedder`` protocol
(see ``seekbase._types``); these are opt-in.

v1 ships ``ApiEmbedder`` (``pip install seekbase[api]``). A local
sentence-transformers embedder is a TODO (DESIGN §10).
"""
from __future__ import annotations

from .api import ApiEmbedder

__all__ = ["ApiEmbedder"]
