"""FileMirror — the canonical file layer (docs/works/store.md).

Every table auto-mirrors to ``<data_dir>/files/ds=YYYYMMDD/<table>.jsonl``,
append-only, one line per write event:

- insert → the full row (business columns + ds/created_at) as one JSON line.
- delete → a tombstone ``{"_deleted": <pk>, "deleted_at": …}`` line in the
  delete-day partition.

Files are canonical; DuckDB/LanceDB are derived and can be rebuilt by replaying
these logs in ds order. Writes are serialized by the single-writer bridge, so
appends don't interleave; each append is flushed + fsync'd.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator


class FileMirror:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, ds: str, table: str) -> Path:
        return self._root / f"ds={ds}" / f"{table}.jsonl"

    def append(self, ds: str, table: str, record: dict) -> None:
        p = self._path(ds, table)
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def purge(self, table: str, pk_field: str, dead: set) -> None:
        """Drop every line (insert or tombstone) whose pk is in ``dead`` from all
        of this table's jsonl files; delete emptied files and ds partitions.
        Used by vacuum — the one operation that physically removes history."""
        dead = {str(k) for k in dead}
        if not dead or not self._root.exists():
            return
        for d in sorted(self._root.iterdir()):
            if not (d.is_dir() and d.name.startswith("ds=")):
                continue
            path = d / f"{table}.jsonl"
            if not path.exists():
                continue
            kept: list[str] = []
            changed = False
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        kept.append(raw if raw.endswith("\n") else raw + "\n")
                        continue
                    key = rec.get("_deleted", rec.get(pk_field))
                    if str(key) in dead:
                        changed = True
                        continue
                    kept.append(raw if raw.endswith("\n") else raw + "\n")
            if not changed:
                continue
            if kept:
                tmp = path.with_name(path.name + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            else:
                path.unlink()
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass

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
