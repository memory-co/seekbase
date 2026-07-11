"""struct — every data-object definition passed between layers, in one place.

Plain frozen dataclasses / type aliases with no behavior beyond (de)serialization,
so it's obvious what shape crosses each boundary. (Behavior lives elsewhere:
schema *parsing* in ``schema.py``, routing in ``api/``, DI wiring in ``service/``.)

  request.py   Request                    op unit (port → executor)
  ticket.py    Ticket                     write receipt (service → port / wire)
  schema.py    Column / TableSpec / Schema parsed table shape + ds/… metadata cols
  row.py       Row / Hit                  query-result dict shapes
"""
from __future__ import annotations

from .request import Request
from .row import Hit, Row
from .schema import (
    CREATED_AT,
    DELETED_AT,
    DELETED_DS,
    DS,
    META_COLUMNS,
    Column,
    Schema,
    TableSpec,
)
from .ticket import Ticket

__all__ = [
    "Request", "Ticket", "Row", "Hit",
    "Column", "TableSpec", "Schema",
    "DS", "CREATED_AT", "DELETED_DS", "DELETED_AT", "META_COLUMNS",
]
