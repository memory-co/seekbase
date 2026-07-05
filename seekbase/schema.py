"""Declarative SCHEMA → internal Schema/TableSpec.

A SCHEMA is an **ordered list** of table specs (table-creation order = list
order); each spec is ``{"table": name, "columns": [{"name","type"}, ...],
"primary": col, "searchable": [...]}``. Design: docs/works/schema.md.

Engine-managed metadata columns — ``ds`` / ``created_at`` / ``deleted_ds`` /
``deleted_at`` — are auto-added to every table (the time machine, DESIGN §6.5);
callers must not declare them. There is no ``files`` field: every table is
mirrored automatically (design; the mirror lands in M2).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ._types import SchemaError

# scalar declared type -> DuckDB type
_SCALAR_SQL = {
    "str": "VARCHAR",
    "int": "BIGINT",
    "float": "DOUBLE",
    "bool": "BOOLEAN",
    "timestamptz": "TIMESTAMP WITH TIME ZONE",
    "json": "JSON",
}
_DECIMAL_RE = re.compile(r"^decimal\((\d+),(\d+)\)$")

# types allowed as a primary key
_PRIMARY_OK = {"str", "int"}

# engine-managed metadata columns (VARCHAR: ISO / YYYYMMDD strings, grep-friendly)
DS = "ds"
CREATED_AT = "created_at"
DELETED_DS = "deleted_ds"
DELETED_AT = "deleted_at"
META_COLUMNS = (DS, CREATED_AT, DELETED_DS, DELETED_AT)
_RESERVED = set(META_COLUMNS)


def _sql_type(decl: str) -> str:
    if decl in _SCALAR_SQL:
        return _SCALAR_SQL[decl]
    m = _DECIMAL_RE.match(decl)
    if m:
        p, s = int(m.group(1)), int(m.group(2))
        if not (1 <= p and 0 <= s <= p):
            raise SchemaError(f"illegal decimal({p},{s}): need 1<=p and 0<=s<=p")
        return f"DECIMAL({p},{s})"
    raise SchemaError(
        f"unknown type {decl!r} (expected str/int/float/bool/decimal(p,s)/"
        "timestamptz/json)"
    )


@dataclass(frozen=True)
class Column:
    name: str
    type: str          # canonical declared type string

    @property
    def sql_type(self) -> str:
        return _sql_type(self.type)


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
        return name in self.column_names or name in _RESERVED


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


def _parse_columns(table: str, raw) -> tuple[Column, ...]:
    if not isinstance(raw, list) or not raw:
        raise SchemaError(f"{table}.columns: must be a non-empty list of {{name, type}}")
    cols: list[Column] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or "name" not in entry or "type" not in entry:
            raise SchemaError(f"{table}.columns[{i}]: need {{'name': …, 'type': …}}")
        name = entry["name"]
        if name in _RESERVED:
            raise SchemaError(
                f"{table}.{name}: {name!r} is engine-managed; do not declare it"
            )
        if name in seen:
            raise SchemaError(f"{table}: duplicate column {name!r}")
        seen.add(name)
        _sql_type(entry["type"])  # validate type now
        cols.append(Column(name=name, type=entry["type"]))
    return tuple(cols)


def _parse_table(entry, index: int) -> TableSpec:
    if not isinstance(entry, dict) or "table" not in entry:
        raise SchemaError(f"SCHEMA[{index}]: each item needs a 'table' name")
    table = entry["table"]

    unknown = set(entry) - {"table", "columns", "primary", "searchable"}
    if unknown:
        raise SchemaError(f"{table}: unknown key(s) {sorted(unknown)}")

    if "columns" not in entry:
        raise SchemaError(f"{table}: missing 'columns'")
    columns = _parse_columns(table, entry["columns"])
    by_name = {c.name: c for c in columns}

    if "primary" not in entry:
        raise SchemaError(f"{table}: missing 'primary' (primary-key column name)")
    primary = entry["primary"]
    if primary not in by_name:
        raise SchemaError(f"{table}.primary: {primary!r} is not a declared column")
    if by_name[primary].type not in _PRIMARY_OK:
        raise SchemaError(
            f"{table}.primary: key must be str/int, got {by_name[primary].type!r}"
        )

    searchable = tuple(entry.get("searchable", ()))
    for s in searchable:
        if s not in by_name:
            raise SchemaError(f"{table}.searchable: {s!r} is not a declared column")
        if by_name[s].type != "str":
            raise SchemaError(f"{table}.searchable: {s!r} must be a str column")

    return TableSpec(
        name=table, columns=columns, primary_key=primary, searchable=searchable
    )


def parse_schema(raw) -> Schema:
    """Validate a raw SCHEMA (ordered list) → :class:`Schema`."""
    if not isinstance(raw, list) or not raw:
        raise SchemaError("SCHEMA must be a non-empty list of table specs")
    tables: list[TableSpec] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        spec = _parse_table(entry, i)
        if spec.name in seen:
            raise SchemaError(f"duplicate table {spec.name!r}")
        seen.add(spec.name)
        tables.append(spec)
    return Schema(tables=tuple(tables))
