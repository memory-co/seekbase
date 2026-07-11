"""The Embedder port and the error hierarchy.

The data objects (Row/Hit/Ticket/Request/Schema…) live in ``struct/``; this
module holds the injection *contract* (Embedder) and the exception types — the
behavioral surface, not data. No business meaning lives here.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    """Injection contract. seekbase never bundles a model into the core;
    it only knows this shape. ``embed`` may be sync or async (a coroutine
    return is awaited internally). The caller of seekbase never touches
    vectors — only text goes in through ``search()`` / ``searchable`` columns.
    """

    @property
    def dim(self) -> int:
        """Vector dimension produced by :meth:`embed`."""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. May return the list directly or an
        awaitable resolving to it."""
        ...


# ─── error hierarchy ───────────────────────────────────────────────────

class SeekbaseError(Exception):
    """Base for all seekbase failures."""


class SeekbaseUnavailable(SeekbaseError):
    """Underlying store could not be opened/served. The host should map
    this to a 503 / degraded mode rather than crashing."""


class SchemaError(SeekbaseError):
    """Declared SCHEMA failed validation at ``open`` time."""


class EmbedderInvalid(SeekbaseError):
    """Embedder is missing when required, or its dim/contract is wrong."""


class ReadOnlyError(SeekbaseError):
    """A non-read statement was passed to :meth:`Seekbase.query`."""


class QueryError(SeekbaseError):
    """Malformed query — unknown table/column, or an unsupported operator."""


class NotFound(SeekbaseError):
    """A ticket (or other addressed object) does not exist. Maps to 404."""
