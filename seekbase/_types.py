"""seekbase value types, the Embedder port, and the error hierarchy.

Plain carriers — no business meaning (no card/round/session) lives here.
Rows and hits are plain dicts by design (§4.2/§4.6); the optional pydantic
binding is deferred (DESIGN §10).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# A row is a plain dict; a Hit is a Row plus a "_score" float key.
Row = dict[str, Any]
Hit = dict[str, Any]


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


class NotSupportedYet(SeekbaseError):
    """A designed capability not yet implemented in this milestone."""
