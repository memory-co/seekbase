"""StreamService — streaming ingestion (docs/works/pipeline-streaming.md).

A stream is a **resident pipeline with an unbounded source**:

    watch '<glob>' [| <bash middles…>] | ingest <table>

Its only job is continuous ingestion — landed rows are queried with normal
bounded SQL (stream writes, query reads, cleanly separated). No stream engine
is built: the source follows files, middles are real subprocess chains, the
sink goes through the ordinary write path (files-first + index + tickets).

Delivery is **at-least-once + an idempotent sink** (§7 of the doc):

- checkpoint = per-file byte offsets, persisted only **after** a micro-batch
  has landed — a crash between land and commit replays that batch, and the
  primary-key dedup in the sink absorbs it (write-once pk → skip existing).
- middles run **batch-scoped**: each micro-batch is piped through a fresh
  subprocess chain (stdin closed → EOF → collect stdout). This deviates from
  the doc's resident-process picture *deliberately*: a resident chain buffers
  an unknowable number of in-flight lines between source and sink, which makes
  post-land offset commits impossible without line provenance. Batch-scoped
  chains give exact at-least-once now; a resident chain is a later
  optimization once provenance exists.

Streams are embedded-only for now (no HTTP surface).
"""
from __future__ import annotations

import asyncio
import contextlib
import glob as _glob
import json
from pathlib import Path

from .._types import QueryError
from ..operator import Registry
from ..operator.base import split_args
from ..operator.policy import Policy
from .pipeline_service import _LEAD, split_pipeline
from .store_service import _jsonl_to_rows, _run_bash_chain

__all__ = ["StreamService", "StreamHandle"]


class StreamHandle:
    """A running stream. ``stop()`` drains the current batch, commits the
    final checkpoint, and returns."""

    def __init__(self, name: str, task: asyncio.Task, stopper) -> None:
        self.name = name
        self._task = task
        self._stop = stopper

    @property
    def running(self) -> bool:
        return not self._task.done()

    async def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    def exception(self):
        """The stream's terminal error, if it died (None while running/clean)."""
        if self._task.done() and not self._task.cancelled():
            return self._task.exception()
        return None


class StreamService:
    def __init__(self, write, schema, registry: Registry, policy: Policy,
                 streams_dir: Path) -> None:
        self._write = write                      # WriteService — the sink's landing path
        self._schema = schema
        self._registry = registry
        self._policy = policy
        self._dir = Path(streams_dir)
        self._streams: dict[str, StreamHandle] = {}

    # ─── compile: watch | [bash middles…] | ingest ──────────────────────

    def _compile(self, pipeline: str):
        segments = split_pipeline(pipeline)
        if len(segments) < 2:
            raise QueryError("a stream needs at least `watch '<glob>' | ingest <table>`")
        parsed = []
        for seg in segments:
            m = _LEAD.match(seg)
            op = self._registry.resolve(m.group(1)) if m else None
            if op is None:
                raise QueryError(
                    "a stream pipeline holds only operators (SQL runs on the landed "
                    f"table via db.query): bad segment {seg[:40]!r}")
            self._policy.check(op)               # ★ same compile-time authorization
            args = op.parse_args(split_args(seg[m.end():]))
            parsed.append((op, args))

        head_op, head_args = parsed[0]
        if head_op.bounded:
            raise QueryError(f"a stream's source must be unbounded (got {head_op.name!r})")
        tail_op, tail_args = parsed[-1]
        if tail_op.name != "ingest":
            raise QueryError("a stream must end in `ingest <table>`")
        self._schema.table(tail_args.table)      # unknown table → SchemaError

        middles = []
        for op, args in parsed[1:-1]:
            if not op.has("optimize_bash"):
                raise QueryError(
                    f"stream middles must be bash-native (got {op.name!r})")
            middles.append(op.optimize_bash(args))
        return head_args, middles, tail_args

    # ─── run: follow files → batch → chain → land → checkpoint ──────────

    async def start_stream(self, pipeline: str, *, name: str) -> StreamHandle:
        if name in self._streams and self._streams[name].running:
            raise QueryError(f"stream {name!r} is already running")
        watch_args, middles, ingest_args = self._compile(pipeline)
        stop = asyncio.Event()
        task = asyncio.create_task(
            self._run(name, watch_args, middles, ingest_args, stop))
        handle = StreamHandle(name, task, stop)
        self._streams[name] = handle
        return handle

    async def _run(self, name, watch_args, middles, ingest_args, stop: asyncio.Event):
        ckpt_path = self._dir / f"{name}.json"
        offsets: dict[str, int] = {}
        if ckpt_path.exists():
            with contextlib.suppress(ValueError, OSError):
                offsets = {k: int(v) for k, v in json.loads(ckpt_path.read_text()).items()}

        flush_s = ingest_args.flush_ms / 1000.0
        poll_s = watch_args.poll_ms / 1000.0
        while not stop.is_set():
            lines, batch_offsets = self._read_new_lines(
                watch_args.glob, offsets, ingest_args.batch)
            if lines:
                landed = await self._land(lines, middles, ingest_args)
                offsets.update(batch_offsets)    # ★ commit only AFTER the batch landed
                self._checkpoint(ckpt_path, offsets)
                if landed and not stop.is_set():
                    continue                     # drain hot files without sleeping
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=max(poll_s, flush_s))
        # graceful stop: one last sweep so appended-but-unread lines land
        lines, batch_offsets = self._read_new_lines(
            watch_args.glob, offsets, ingest_args.batch)
        if lines:
            await self._land(lines, middles, ingest_args)
            offsets.update(batch_offsets)
            self._checkpoint(ckpt_path, offsets)

    def _read_new_lines(self, pattern: str, offsets: dict[str, int],
                        max_lines: int) -> tuple[list[bytes], dict[str, int]]:
        """Read up to ``max_lines`` complete new lines past each file's
        checkpointed offset. Only whole (newline-terminated) lines advance the
        offset — a half-written tail line is left for the next poll."""
        lines: list[bytes] = []
        new_offsets: dict[str, int] = {}
        for path in sorted(_glob.glob(pattern, recursive=True)):
            if len(lines) >= max_lines:
                break
            start = offsets.get(path, 0)
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    chunk = f.read()
            except OSError:
                continue
            pos = start
            for raw in chunk.splitlines(keepends=True):
                if not raw.endswith(b"\n") or len(lines) >= max_lines:
                    break
                if raw.strip():
                    lines.append(raw)
                pos += len(raw)
            if pos > start:
                new_offsets[path] = pos
        return lines, new_offsets

    async def _land(self, lines: list[bytes], middles: list[list[str]],
                    ingest_args) -> int:
        data = b"".join(lines)
        if middles:                              # batch-scoped chain: EOF-driven, exact
            data = await asyncio.to_thread(
                _run_bash_chain, middles, data, self._policy.exec_timeout)
        rows = _jsonl_to_rows(data)
        if not rows:
            return 0
        await self._write.insert(
            ingest_args.table, rows, skip_existing=True)   # ★ idempotent sink: pk dedup
        return len(rows)

    def _checkpoint(self, path: Path, offsets: dict[str, int]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(offsets))
        tmp.replace(path)                        # atomic swap

    async def close(self) -> None:
        for handle in list(self._streams.values()):
            if handle.running:
                await handle.stop()
