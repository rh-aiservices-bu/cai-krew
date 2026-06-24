# mem0 Inference Flow — Full Detail

This document traces every step mem0 executes when you call `POST /memories` with
`infer: true`. It is derived directly from the mem0 OSS source code
(`mem0/memory/main.py`, `mem0/memory/storage.py`, `mem0/configs/prompts.py`).

---

## Storage components

mem0 uses **three separate stores** that each serve a different purpose.

| Store | Technology | Purpose |
|---|---|---|
| `memories` | pgvector collection | The long-term memory store — extracted facts |
| `memories_entities` | pgvector collection (separate) | Entity index for name/concept boosting in search |
| SQLite (`HISTORY_DB_PATH`) | SQLite file (`/tmp/mem0-history.db` in our deployment) | Two tables: rolling message window + ADD/UPDATE audit log |

The entity store is **lazily initialised** on first use. It shares the same pgvector
connection but targets a different collection name (`memories_entities`).

---

## Entry point

```
POST /memories
  → server: add_memory()
    → SDK: memory.add()
      → SDK: _add_to_vector_store()
```

Before reaching the pipeline, `add()` does:

1. **Input normalisation** — a bare string becomes
   `[{"role": "user", "content": "<string>"}]`; a single dict is wrapped in a list.
2. **Filter + metadata construction** — `user_id`, `agent_id`, `run_id` are validated
   (at least one required) and merged with any supplied `metadata` dict.
3. **Vision parsing** — image content is stripped unless `enable_vision` is configured.

---

## `infer: false` path

The LLM is **not called at all**.

For each message in the list (system messages are silently skipped):

1. Embed the raw `content` string.
2. Insert directly into pgvector `memories` with role metadata.
3. Messages are **not** saved to the SQLite history.
4. No deduplication, no entity linking.

Use this path (via `mem0_conclude`) when you want to store a verbatim
fact without any extraction or processing.

---

## `infer: true` path — V3 Phased Batch Pipeline

This is the full pipeline. All eight phases are sequential within a single
synchronous HTTP request, which is why it can take 30–60 s with a large LLM.

---

### Phase 0 — Context gathering

```python
session_scope = _build_session_scope(filters)
# → deterministic string, e.g.:
#   "agent_id=hermes&run_id=abc-123&user_id=agent:hermes|user:Erwan"
#   (keys always sorted alphabetically)

last_messages = self.db.get_last_messages(session_scope, limit=10)
parsed_messages = parse_messages(messages)   # flatten list → single string
```

**`session_scope`** is a URL-like query string built from every non-null ID
(`user_id`, `agent_id`, `run_id`) sorted alphabetically and joined with `&`.
It is the SQLite primary key for the message history.

**`last_messages`** is fetched from the `messages` SQLite table — up to 10 messages
ordered chronologically for this session scope. These are the raw messages from
*previous* calls in this session (not the current turn). They provide rolling
conversational context for the extraction LLM without you having to resend history
yourself.

**`parsed_messages`** is the current turn concatenated into a plain string for
embedding.

---

### Phase 1 — Existing memory retrieval

```python
query_embedding = embedding_model.embed(parsed_messages, "search")
existing_results = vector_store.search(
    query=parsed_messages,
    vectors=query_embedding,
    top_k=10,
    filters=search_filters,   # only user_id / agent_id / run_id keys
)
```

mem0 embeds the current conversation turn and performs a **semantic vector search**
against the `memories` pgvector collection filtered to this actor/session. The top 10
most-similar existing memories are retrieved.

These are then **remapped to sequential integer IDs** (`"0"`, `"1"`, …) before being
sent to the LLM:

```python
existing_memories = [{"id": "0", "text": "..."}, {"id": "1", "text": "..."}, ...]
```

This anti-hallucination measure prevents the LLM from inventing or mutating UUIDs.
The mapping back to real UUIDs is held in `uuid_mapping` on the Python side.

---

### Phase 2 — LLM extraction (single call)

This is the only LLM call in the entire pipeline.

**System prompt**: `ADDITIVE_EXTRACTION_PROMPT` — a ~600-line prompt that defines the
extraction role, all input sections, quality standards, output format, and examples.
The LLM's job is exclusively ADD: identify every memorable fact and output them as
self-contained statements. A suffix `AGENT_CONTEXT_SUFFIX` is appended when the
request is scoped only to `agent_id` (no `user_id`).

**User prompt** is assembled by `generate_additive_extraction_prompt()` with these
sections (in order):

| Section | Content |
|---|---|
| `## Summary` | Narrative user profile from prior conversations (populated by cloud; usually empty in OSS) |
| `## Last k Messages` | The last 10 messages from SQLite (Phase 0) — prior turns in this session |
| `## Recently Extracted Memories` | Memories already extracted earlier in this same session (dedup reference) |
| `## Existing Memories` | Top-10 semantically similar memories from Phase 1 (integer-ID mapped) |
| `## New Messages` | The current conversation turn you just sent |
| `## Observation Date` | When the conversation is happening — used to ground relative time references ("yesterday", "last week") to absolute dates |
| `## Current Date` | Today's system date — NOT used to interpret message content |
| `## Custom Instructions` | Your `prompt` field value — **highest priority**, overrides all other naming/formatting defaults |

The LLM is called with `response_format={"type": "json_object"}` and must return:

```json
{
  "memory": [
    {"text": "Erwan likes apples.", "linked_memory_ids": []},
    {"text": "Erwan works as a software engineer in Paris.", "linked_memory_ids": ["3"]}
  ]
}
```

`linked_memory_ids` references integer IDs from the existing memories list, allowing
the LLM to signal "this new fact is related to existing memory #3".

If the LLM call fails **or** returns an empty `memory` array → messages are saved to
SQLite and `[]` is returned immediately. No further phases run.

---

### Phase 3 — Batch embedding

All extracted memory texts are embedded in a **single batch call** to the embedding
model (Nomic-embed-text-v2-moe, 768 dims in our deployment). Falls back to individual
embeds if the batch call fails.

---

### Phase 4 — Hash deduplication

Each extracted memory text is MD5-hashed. This hash is checked against:

1. Hashes of the existing memories retrieved in Phase 1.
2. Hashes of other memories already processed in this same batch.

Exact-text duplicates are silently skipped. Note: this is **exact-hash dedup** —
semantically similar but not identical memories are not caught here (that's handled
by Phase 1 sending existing memories to the LLM, which should decide to not re-extract
them).

Each surviving record also gets `text_lemmatized` computed (for BM25 hybrid search).

---

### Phase 5 — Metadata assembly

Each memory record is assembled:

```python
{
    "data":             "<extracted text>",
    "hash":             "<md5>",
    "text_lemmatized":  "<lemmatized text for BM25>",
    "created_at":       "<ISO timestamp>",
    "updated_at":       "<same as created_at on ADD>",
    "user_id":          "<actor_key or __team__>",
    "agent_id":         "<group_id>",
    "run_id":           "<session_id>",
    # + any metadata fields you sent in the request (scope, actors, created_at from our plugin)
}
```

---

### Phase 6 — Batch persist to pgvector

All records are inserted into the `memories` collection in a **single batch**
`vector_store.insert()` call. Falls back to per-record inserts on failure.

Alongside this, an ADD event is written to the SQLite `history` table for each
memory:

```
history(memory_id, old_memory=NULL, new_memory="...", event="ADD", created_at, is_deleted=0)
```

This audit log is what powers the "Memory History" view in the dashboard.

---

### Phase 7 — Entity linking (spaCy NER)

Requires spaCy with `en_core_web_sm` to be installed (our `mem0-server-nlp` image
adds this via the chained OpenShift build).

For every newly stored memory text:

1. **Named entity recognition** via spaCy — extracts entities like `PERSON`,
   `ORG`, `GPE`, `LOC`, `PRODUCT`, `EVENT`, etc.
2. **Global dedup** — the same entity appearing in multiple memories in this
   batch is deduplicated before embedding.
3. **Batch embed** all unique entity strings.
4. **Search** the `memories_entities` collection for each entity (threshold 0.95):
   - **Match found** → update the existing entity record: append the new `memory_id`
     to its `linked_memory_ids` list.
   - **No match** → insert a new entity record:
     ```json
     {
       "data": "Erwan",
       "entity_type": "PERSON",
       "linked_memory_ids": ["<memory-uuid>"],
       "user_id": "...", "agent_id": "...", "run_id": "..."
     }
     ```

This entity index is what produces the `entity_boost` field you see in search
results when using `explain: true`. When you search for "apples", if "Erwan" is an
entity linked to an apple-related memory, that memory gets a score boost.

Entity linking failure is non-fatal — a warning is logged and the pipeline continues.

---

### Phase 8 — Save messages + return

```python
self.db.save_messages(messages, session_scope)
```

The current turn's messages are appended to the SQLite `messages` table under the
`session_scope` key. An immediate eviction runs to keep only the **10 most recent**
messages for this scope:

```sql
DELETE FROM messages WHERE session_scope = ? AND id NOT IN (
    SELECT id FROM (
        SELECT id FROM messages WHERE session_scope = ? ORDER BY created_at DESC LIMIT 10
    )
)
```

So the table never grows beyond 10 messages per session scope.

Finally, `[{"id": "...", "memory": "...", "event": "ADD"}, ...]` is returned to the
server, which wraps it in `{"results": [...]}` and sends the HTTP response.

---

## Complete sequence diagram

```
Client (our plugin _bg_sync)
  │
  ▼
POST /memories  {messages, user_id, agent_id, run_id, infer:true, prompt}
  │
  ▼
server: add_memory()
  │  validate ≥1 identifier
  │  normalise messages
  │
  ▼
memory.add()  →  _add_to_vector_store()
  │
  ├─ Phase 0: build session_scope
  │           fetch last 10 msgs from SQLite  ◄─── rolling history (per run_id)
  │
  ├─ Phase 1: embed current turn
  │           vector search existing memories (top_k=10)
  │           map UUIDs → integers
  │
  ├─ Phase 2: build LLM user-prompt
  │             § Summary (usually empty)
  │             § Last k Messages  ◄── from Phase 0
  │             § Recently Extracted (this session)
  │             § Existing Memories ◄── from Phase 1
  │             § New Messages      ◄── current turn
  │             § Observation/Current Date
  │             § Custom Instructions  ◄── our prompt field
  │           call LLM (Qwen3.6-35B via LiteLLM)
  │           parse JSON response → extracted_memories[]
  │           (if empty → save msgs to SQLite, return [])
  │
  ├─ Phase 3: batch embed extracted texts
  │
  ├─ Phase 4: MD5 hash dedup vs existing + within batch
  │
  ├─ Phase 5: assemble metadata per record
  │
  ├─ Phase 6: batch insert into pgvector `memories`
  │           write ADD events to SQLite `history`
  │
  ├─ Phase 7: spaCy NER on all new memory texts
  │           batch embed unique entities
  │           upsert into pgvector `memories_entities`
  │             → links entity ↔ memory_id for search boosting
  │
  └─ Phase 8: save current messages to SQLite (evict >10)
              return [{"id","memory","event":"ADD"}, ...]
  │
  ▼
{"results": [...]}  →  HTTP response to plugin
```

---

## How our plugin fits in

| Plugin field | mem0 phase it affects |
|---|---|
| `user_id` (actor key) | Filters in Phase 1 search; stored in metadata (Phase 5) |
| `agent_id` (group id) | Same as above; also selects `AGENT_CONTEXT_SUFFIX` if user_id absent |
| `run_id` (session id) | Part of `session_scope` → scopes SQLite history (Phase 0) and vector filters |
| `prompt` (custom instructions) | Appended as `## Custom Instructions` in Phase 2 — highest priority |
| `metadata` (scope, actors, created_at) | Stored verbatim in each memory's pgvector payload (Phase 5) |
| `infer: false` (`mem0_conclude`) | Skips Phases 0–7 entirely; stores raw message content verbatim |
| `_write_client` (90s timeout) | Ensures the HTTP client doesn't abandon the request before the LLM finishes |

---

## Key things that were not obvious

**The SQLite message history is per `run_id`.**
Each new Hermes session gets a fresh `run_id`, so the rolling context window resets.
Cross-session knowledge lives only in the extracted memories in pgvector — not in raw
messages. Within a session, by turn 5 (10 messages), the LLM has full rolling context
automatically. You do not need to resend history yourself.

**The LLM only runs once per `add()` call.**
There is no separate ADD/UPDATE/DELETE decision step in V3 — the extraction prompt
was redesigned to be additive-only. The LLM extracts facts in a single call and the
result is always ADD. UPDATE and DELETE of existing memories are not triggered by the
current OSS pipeline (the old two-step approach was replaced).

**Entity linking is a second pgvector collection.**
The `memories_entities` table is distinct from `memories`. It holds entity records
(names, places, orgs) linked to memory UUIDs. Search queries use it to boost scores
for memories containing relevant entities — this is the `entity_boost` field you see
with `explain: true`.

**`{"results": []}` does not always mean failure.**
If the LLM decides there is nothing new to extract (all facts are duplicates or the
conversation contains no memorable information), it legitimately returns an empty list.
The messages are still saved to SQLite history and the request is considered
successful.

**The OpenShift route has a 30s default timeout.**
The two embedding calls + one LLM call can exceed this, causing a 504 even when
processing succeeds server-side (the server keeps running, the memory gets stored,
but the HTTP response is dropped). Fix: annotate the route with
`haproxy.router.openshift.io/timeout: 120s`.
