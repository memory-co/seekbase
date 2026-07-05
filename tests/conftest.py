"""Shared fixtures + helpers for seekbase scenarios.

Scenarios organised the way memory.talk does it: one directory per scenario,
each with its own ``README.md`` (what it tests / doesn't / which fixtures) and
``test.py``. This module is the cross-scenario toolbox.

- ``db`` (fixture) — a standard embedded ``Seekbase`` with the canonical
  ``cards`` schema; auto-closed. For the plain happy-path scenarios.
- ``pair`` (fixture) — ``(server_db, client)``: an embedded server plus an HTTP
  client bound to it in-process (no port, no runner); both auto-closed.
- ``open_db`` / ``serve_pair`` (helpers) — for scenarios that need a custom
  schema, embedder, ``as_of`` or auth, built inline with their own try/finally.
- ``FakeEmbedder`` — deterministic, dependency-free; satisfies searchable-column
  wiring without a real model or network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

# Let `from tests.conftest import ...` resolve when running from the repo root
# (safety net; editable install already exposes `seekbase`).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seekbase import Seekbase                    # noqa: E402
from seekbase.server import seekbase_server      # noqa: E402

# Canonical schema: a `cards` table with one searchable column, so both the
# structured path and the embedder wiring are exercised by default.
SCHEMA = {
    "cards": {
        "columns": {"card_id": "str primary", "issue": "str", "kind": "str", "n": "int"},
        "searchable": ["issue"],
    },
}


class FakeEmbedder:
    """Deterministic, dependency-free embedder — enough to open searchable
    schemas without a real model. Vectors aren't exercised until M3."""

    dim = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t))] * self.dim for t in texts]


async def open_db(data_root, *, schema=SCHEMA, embedder="fake", as_of=None):
    """Open an embedded Seekbase under ``data_root``. Defaults to the canonical
    SCHEMA + a FakeEmbedder (pass ``embedder=None`` to test the missing case)."""
    if embedder == "fake":
        embedder = FakeEmbedder()
    return await Seekbase.open(
        Path(data_root) / "db", schema=schema, embedder=embedder, as_of=as_of
    )


def client_for(server_db, *, app_key=None, client_key=None, as_of=None):
    """An HTTP client bound to ``server_db`` via in-process ASGI. ``app_key`` is
    the server's required token, ``client_key`` the one the client sends (differ
    them to test auth failure)."""
    transport = httpx.ASGITransport(app=seekbase_server(server_db, api_key=app_key))
    return Seekbase.connect(
        "http://server", api_key=client_key, as_of=as_of, transport=transport
    )


async def serve_pair(data_root, *, api_key=None, as_of=None):
    """(server_db, client) with matching auth — the plain server round-trip."""
    server_db = await open_db(data_root)
    client = await client_for(server_db, app_key=api_key, client_key=api_key, as_of=as_of)
    return server_db, client


@pytest_asyncio.fixture
async def db(tmp_path):
    d = await open_db(tmp_path)
    try:
        yield d
    finally:
        await d.close()


@pytest_asyncio.fixture
async def pair(tmp_path):
    server_db, client = await serve_pair(tmp_path)
    try:
        yield server_db, client
    finally:
        await client.close()
        await server_db.close()
