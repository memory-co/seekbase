"""Declarative SCHEMA → ``struct.Schema`` (parse + validate + type mapping).

A SCHEMA is an **ordered list** of table specs (table-creation order = list
order); each spec is ``{"table": name, "columns": [{"name","type"}, ...],
"primary": col, "searchable": [...]}``. Design: docs/works/schema.md.

The data objects (``Column`` / ``TableSpec`` / ``Schema`` + the ds/… metadata
columns) live in ``struct/`` and are re-exported here; this module only parses
raw input and resolves each column's DuckDB ``sql_type``. Engine-managed
metadata columns must not be declared by callers.
"""
from __future__ import annotations

import re

from ._types import SchemaError
from .struct import (
    CREATED_AT,
    DELETED_AT,
    DELETED_DS,
    DS,
    META_COLUMNS,
    Column,
    Schema,
    TableSpec,
)

__all__ = [
    "parse_schema", "Column", "TableSpec", "Schema",
    "DS", "CREATED_AT", "DELETED_DS", "DELETED_AT", "META_COLUMNS",
]

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
        cols.append(Column(name=name, type=entry["type"], sql_type=_sql_type(entry["type"])))
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
