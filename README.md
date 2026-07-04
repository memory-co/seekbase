# seekbase

A supabase-style data port with semantic **`search()` as a first-class operator** — DuckDB for the structured/analytical side, LanceDB for vectors, a local file mirror for audit. One port, one directory, zero ops.

**Two usage forms, one identical API:**

| Form | Open with | Runs | Use when |
|---|---|---|---|
| **Embedded** | `Seekbase.open(dir, schema=…)` | in-process, on DuckDB | single process, local-first, zero ops |
| **Server** | `Seekbase.connect(url)` → talks to a running server | over HTTP | many clients / processes share one instance |

Calling code is byte-for-byte the same between them — the `table().select()…` chain and `search()` don't change; only how you obtain the `db` handle does.

> **Status: early scaffold (M1).** The structured ORM (`select` / `insert` / tombstone-`delete` / `count`), read-only SQL passthrough, a partial as-of time machine, **and both usage forms** run today. Vector `search()`, the outbox, the file mirror, `rebuild()` and `vacuum()` land in later milestones. See [DESIGN.md](DESIGN.md) for the full plan.

## Install

```bash
pip install seekbase            # embedded + HTTP client + ApiEmbedder, out of the box
pip install 'seekbase[server]'  # + uvicorn, to run the server on a port
```

The HTTP **client** (`Seekbase.connect`) needs only the core (httpx is a core dep). The `[server]` extra is only for the process that *serves* the port.

## Embedded (in-process)

```python
from seekbase import Seekbase

SCHEMA = {
    "cards": {
        "columns": {"card_id": "str primary", "issue": "str",
                    "kind": "str", "created_at": "str"},
        "searchable": ["issue"],                 # (used once the vector engine lands)
    },
}

db = await Seekbase.open("./data", schema=SCHEMA)

await db.table("cards").insert({"card_id": "c1", "issue": "pty vs tmux", "kind": "issue"})

rows = await (db.table("cards")
    .select("card_id", "issue")
    .eq("kind", "issue")
    .order("created_at", desc=True)
    .limit(20))

await db.table("cards").delete().eq("card_id", "c1")   # tombstone, never physical delete

await db.close()
```

## Server (HTTP)

Start a server — it holds the schema (and, later, the embedder) and owns the data directory:

```python
# serve.py
from seekbase import Seekbase
from seekbase.server import serve

db = await Seekbase.open("./data", schema=SCHEMA)   # same embedded db as above
serve(db, host="0.0.0.0", port=8000, api_key="secret")   # blocking; needs seekbase[server]
```

`create_app(db)` returns a plain ASGI app if you'd rather run it under your own server (uvicorn/hypercorn/…) or mount it in a larger app.

Then talk to it from anywhere — **the exact same calling code as embedded**, only the handle changes:

```python
db = await Seekbase.connect("http://localhost:8000", api_key="secret")

await db.table("cards").insert({"card_id": "c1", "issue": "pty vs tmux", "kind": "issue"})
rows = await db.table("cards").select("card_id", "issue").eq("kind", "issue").limit(20)

await db.close()
```

The query chain is serialized to a single `POST /v1/execute`; the server runs it and returns rows. Errors keep their type across the wire (a `ReadOnlyError` on the server raises `ReadOnlyError` on the client). Auth is one optional bearer token; time-machine reads work over HTTP too (`Seekbase.connect(url, as_of="2026-06-01T00:00:00Z")`).

## Principles

- **insert-only, engine-enforced**: no `update`/`upsert`; `delete()` only writes a `deleted_at` tombstone. History stays honest — the time machine is rigorous for *all* columns.
- **business-agnostic**: no domain concepts, reads no config — you inject `data_dir`, `schema`, and (for search) an `embedder`.
- **the caller never sees vectors**: declare `searchable` columns; `search(text)` embeds + retrieves + combines with structured filters on the same chain.

Apache-2.0.
