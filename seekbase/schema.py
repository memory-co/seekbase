"""Declarative SCHEMA → internal TableSpec.

A SCHEMA is a dict of ``{table_name: {"columns": {...}, "searchable": [...],
"files": ... }}`` (DESIGN §4.5). This module parses and validates it once at
``open`` time and hands the engines typed specs. ``created_at`` / ``deleted_at``
are engine-managed metadata columns, auto-added to every table (DESIGN §7);
callers must not declare them.

M1 uses ``columns`` (structured engine). ``searchable`` and ``files`` are
parsed and validated here so the spec is stable, but consumed later
(M2 file mirror, M3 vector engine).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ._types import SchemaError

# declared type -> DuckDB type
_TYPE_SQL = {"str": "VARCHAR", "int": "BIGINT", "float": "DOUBLE", "bool": "BOOLEAN"}

# engine-managed metadata columns (ISO-8601 UTC strings, grep-friendly)
CREATED_AT = "created_at"
DELETED_AT = "deleted_at"
_RESERVED = {CREATED_AT, DELETED_AT}


@dataclass(frozen=True)
class Column:
    name: str
    type: str          # one of _TYPE_SQL keys
    primary: bool = False

    @property
    def sql_type(self) -> str:
        return _TYPE_SQL[self.type]


@dataclass(frozen=True)
class FilesSpec:
    """Local JSON/JSONL mirror declaration (consumed in M2)."""
    path_template: str
    mode: str = "json"  # "json" (one file per row) | "jsonl" (append-only)


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: dict[str, Column]          # declared columns only (no metadata)
    primary_key: str
    searchable: tuple[str, ...] = ()    # consumed in M3
    files: FilesSpec | None = None      # consumed in M2

    @property
    def column_names(self) -> list[str]:
        """Declared columns, in declaration order (excludes metadata)."""
        return list(self.columns)

    @property
    def all_column_names(self) -> list[str]:
        """Declared columns + engine-managed metadata."""
        return [*self.columns, CREATED_AT, DELETED_AT]

    def is_column(self, name: str) -> bool:
        return name in self.columns or name in _RESERVED


@dataclass(frozen=True)
class Schema:
    tables: dict[str, TableSpec] = field(default_factory=dict)

    def table(self, name: str) -> TableSpec:
        try:
            return self.tables[name]
        except KeyError:
            raise SchemaError(f"unknown table {name!r}") from None


def _parse_column(table: str, name: str, decl: str) -> Column:
    if name in _RESERVED:
        raise SchemaError(
            f"{table}.{name}: {name!r} is an engine-managed metadata column; "
            "do not declare it"
        )
    parts = decl.split()
    if not parts or parts[0] not in _TYPE_SQL:
        raise SchemaError(
            f"{table}.{name}: type must be one of {sorted(_TYPE_SQL)}, got {decl!r}"
        )
    modifiers = set(parts[1:])
    primary = "primary" in modifiers
    unknown = modifiers - {"primary"}
    if unknown:
        raise SchemaError(f"{table}.{name}: unknown modifier(s) {sorted(unknown)}")
    return Column(name=name, type=parts[0], primary=primary)


def _parse_files(table: str, spec, columns: dict[str, Column]) -> FilesSpec:
    if isinstance(spec, str):
        files = FilesSpec(path_template=spec, mode="json")
    elif isinstance(spec, dict):
        if "path" not in spec:
            raise SchemaError(f"{table}.files: dict form needs a 'path' key")
        mode = spec.get("mode", "json")
        if mode not in {"json", "jsonl"}:
            raise SchemaError(f"{table}.files: mode must be 'json'|'jsonl', got {mode!r}")
        files = FilesSpec(path_template=spec["path"], mode=mode)
    else:
        raise SchemaError(f"{table}.files: must be a str or dict, got {type(spec).__name__}")
    # every {placeholder} must be a real column
    import re
    for ph in re.findall(r"\{(\w+)\}", files.path_template):
        if ph not in columns:
            raise SchemaError(
                f"{table}.files: template placeholder {{{ph}}} is not a declared column"
            )
    return files


def parse_schema(raw: dict) -> Schema:
    """Validate a raw SCHEMA dict → :class:`Schema`. Raises :class:`SchemaError`."""
    if not isinstance(raw, dict) or not raw:
        raise SchemaError("SCHEMA must be a non-empty dict of {table: spec}")

    tables: dict[str, TableSpec] = {}
    for table, spec in raw.items():
        if not isinstance(spec, dict) or "columns" not in spec:
            raise SchemaError(f"{table}: spec must be a dict with a 'columns' key")

        columns: dict[str, Column] = {}
        primaries: list[str] = []
        for name, decl in spec["columns"].items():
            col = _parse_column(table, name, decl)
            columns[name] = col
            if col.primary:
                primaries.append(name)

        if len(primaries) != 1:
            raise SchemaError(
                f"{table}: exactly one column must be 'primary', found {len(primaries)}"
            )

        searchable = tuple(spec.get("searchable", ()))
        for s in searchable:
            if s not in columns:
                raise SchemaError(f"{table}.searchable: {s!r} is not a declared column")

        files = _parse_files(table, spec["files"], columns) if "files" in spec else None

        # reject unknown top-level keys (typo guard)
        unknown = set(spec) - {"columns", "searchable", "files"}
        if unknown:
            raise SchemaError(f"{table}: unknown schema key(s) {sorted(unknown)}")

        tables[table] = TableSpec(
            name=table,
            columns=columns,
            primary_key=primaries[0],
            searchable=searchable,
            files=files,
        )

    return Schema(tables=tables)
