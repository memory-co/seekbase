"""Shared fixtures + helpers for seekbase scenarios (new API).

- ``db`` (fixture) — a standard embedded ``Seekbase`` with the canonical schema.
- ``pair`` (fixture) — ``(server_db, client)``: an embedded server + an HTTP
  client bound to it in-process (no port, no runner); both auto-closed.
- ``open_db`` / ``client_for`` / ``serve_pair`` (helpers) — for custom cases.
- ``FakeEmbedder`` — deterministic, dependency-free (searchable wiring).
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seekbase import Seekbase                    # noqa: E402
from seekbase.server import seekbase_server      # noqa: E402

# Canonical schema (ordered list; a searchable column exercises embedder wiring).
SCHEMA = [
    {
        "table": "cards",
        "columns": [
            {"name": "card_id", "type": "str"},
            {"name": "issue", "type": "str"},
            {"name": "kind", "type": "str"},
            {"name": "n", "type": "int"},
        ],
        "primary": "card_id",
        "searchable": ["issue"],
    },
]


class FakeEmbedder:
    dim = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t))] * self.dim for t in texts]


async def open_db(data_root, *, schema=SCHEMA, embedder="fake"):
    if embedder == "fake":
        embedder = FakeEmbedder()
    return await Seekbase.open(Path(data_root) / "db", schema=schema, embedder=embedder)


def client_for(server_db, *, app_key=None, client_key=None):
    """A client bound to ``server_db`` via in-process ASGI. ``app_key`` is the
    server's required token, ``client_key`` the one the client sends."""
    transport = httpx.ASGITransport(app=seekbase_server(server_db, api_key=app_key))
    return Seekbase.connect("http://server", api_key=client_key, transport=transport)


async def serve_pair(data_root, *, api_key=None):
    server_db = await open_db(data_root)
    client = await client_for(server_db, app_key=api_key, client_key=api_key)
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
