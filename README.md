# seekbase

A supabase-style **embedded** data port with semantic **`search()` as a first-class operator** — DuckDB for the structured/analytical side, LanceDB for vectors, a local file mirror for audit. One port, one directory, zero ops.

> **Status: early scaffold (M1).** The structured ORM (`select` / `insert` / tombstone-`delete` / `count`), read-only SQL passthrough, and a partial as-of time machine run today on DuckDB. Vector `search()`, the outbox, the file mirror, `rebuild()` and `vacuum()` land in later milestones. See [DESIGN.md](DESIGN.md) for the full plan.

## Install

```bash
pip install seekbase          # core (DuckDB ORM)
pip install 'seekbase[api]'   # + ApiEmbedder (OpenAI-compatible /embeddings)
```

## One minute

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

## Principles

- **insert-only, engine-enforced**: no `update`/`upsert`; `delete()` only writes a `deleted_at` tombstone. History stays honest — the time machine is rigorous for *all* columns.
- **business-agnostic**: no domain concepts, reads no config — you inject `data_dir`, `schema`, and (for search) an `embedder`.
- **the caller never sees vectors**: declare `searchable` columns; `search(text)` embeds + retrieves + combines with structured filters on the same chain.

Apache-2.0.
