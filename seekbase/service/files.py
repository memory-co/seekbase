"""FileService — the canonical file-mirror subdomain (docs/works/store.md).

Owns everything about the file tier: the ``<data_dir>/files/ds=YYYYMMDD/<table>.jsonl``
layout and the on-disk record shapes, so no other layer needs to know them.
Append-only, one line per write event:

- insert → ``write_puts``: the full row (business columns + ds/created_at) per line.
- delete → ``write_deletes``: a tombstone ``{"_deleted": <pk>, "deleted_at": …}`` line.

Files are canonical; DuckDB (rows + vss/fts) is derived and rebuilt by replaying
these logs in ds order (``iter_events``). Appends run on the single-writer bridge
so they don't interleave; each is flushed + fsync'd.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

from ..struct import CREATED_AT, DELETED_AT, DS


class FileService:
    def __init__(self, bridge, root: Path) -> None:
        self._bridge = bridge
        self._root = Path(root)

    def _path(self, ds: str, table: str) -> Path:
        return self._root / f"ds={ds}" / f"{table}.jsonl"

    def _append(self, ds: str, table: str, record: dict) -> None:
        p = self._path(ds, table)
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    # ─── write (canonical, files-first; this layer owns the line shape) ─

    async def write_puts(self, spec, records: list[dict], ds: str, now: str) -> None:
        """Append one put line per record: business columns + ds + created_at."""
        lines = [{**{c: rec[c] for c in spec.column_names}, DS: ds, CREATED_AT: now}
                 for rec in records]
        await self._bridge.run(lambda: [self._append(ds, spec.name, m) for m in lines])

    async def write_deletes(self, table: str, keys: list, ds: str, now: str) -> None:
        """Append one tombstone line per deleted key."""
        await self._bridge.run(
            lambda: [self._append(ds, table, {"_deleted": k, DELETED_AT: now}) for k in keys])

    # ─── read (replay source for rebuild) ──────────────────────────────

    def iter_events(self, table: str) -> Iterator[tuple[str, dict]]:
        """Yield ``(ds, record)`` for a table across all ds partitions in ds
        (chronological) order; within a partition, in append (line) order.
        A trailing torn line is tolerated (skipped)."""
        if not self._root.exists():
            return
        ds_dirs = sorted(
            d for d in self._root.iterdir()
            if d.is_dir() and d.name.startswith("ds=")
        )
        for d in ds_dirs:
            ds = d.name[len("ds="):]
            path = d / f"{table}.jsonl"
            if not path.exists():
                continue
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # tolerate a torn trailing line
                    yield ds, rec
