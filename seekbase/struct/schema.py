"""Schema value objects — the parsed shape of a table.

Pure data (no parsing/validation — that's in ``seekbase/schema.py``, which
builds these). ``Column.sql_type`` is the resolved DuckDB type, computed at
parse time and stored, so this module needs no type-mapping logic.
"""
from __future__ import annotations

from dataclasses import dataclass

from .._types import SchemaError

# engine-managed metadata columns (VARCHAR: ISO / YYYYMMDD strings, grep-friendly)
DS = "ds"
CREATED_AT = "created_at"
DELETED_DS = "deleted_ds"
DELETED_AT = "deleted_at"
META_COLUMNS = (DS, CREATED_AT, DELETED_DS, DELETED_AT)


@dataclass(frozen=True)
class Column:
    name: str
    type: str          # declared type string (str / int / decimal(p,s) / …)
    sql_type: str      # resolved DuckDB type (computed by the parser)


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[Column, ...]          # declared columns, in order (no metadata)
    primary_key: str
    searchable: tuple[str, ...] = ()

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def all_column_names(self) -> list[str]:
        return [*self.column_names, *META_COLUMNS]

    def is_column(self, name: str) -> bool:
        return name in self.column_names or name in META_COLUMNS


@dataclass(frozen=True)
class Schema:
    tables: tuple[TableSpec, ...] = ()    # ordered

    def table(self, name: str) -> TableSpec:
        for t in self.tables:
            if t.name == name:
                return t
        raise SchemaError(f"unknown table {name!r}")

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]
