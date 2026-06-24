# Memory System Specification

## Goals

- Persist memories across conversations for a team of users and agents
- Each memory knows who was in the conversation (actors) and whether it is personal or team-wide
- Retrieval is context-aware: same-actor memories rank highest, team memories equally, other actors' memories surface at lower weight
- A dedicated memory agent handles per-turn deduplication and periodic pruning

---

## Storage Model

### Actors

Every memory records the participants in the conversation as a sorted, stable key:

```
actor_key = sorted("type:id") joined by "|"

Example: "agent:flaude|user:Erwan"
```

Supported actor types: `user`, `agent`.
Multi-actor conversations (>2) extend the same key naturally.

### Scope

| Scope      | Meaning                                      |
|------------|----------------------------------------------|
| `personal` | Specific to this actor pair                  |
| `team`     | Shared across all actors in the same group   |

### Mem0 field mapping

| Concept            | Mem0 field  | Value                         |
|--------------------|-------------|-------------------------------|
| Personal memory    | `user_id`   | actor key (e.g. `agent:flaude\|user:Erwan`) |
| Team memory        | `user_id`   | `__team__`                    |
| Group identifier   | `agent_id`  | `MEM0_GROUP_ID` (default: `hermes`) |
| Session            | `run_id`    | Hermes session_id             |

All metadata (scope, actors, created_at) is stored in mem0's `metadata` field.

### Group ID

`MEM0_GROUP_ID` identifies the team/installation. All memories share this as `agent_id`,
enabling cross-actor search via `filters: {agent_id: group_id}` without needing a filterless query.

---

## Search Tiers

Every query runs three searches, results are deduplicated by id and ranked by `score × weight`:

| Tier                  | Filter                                        | Weight |
|-----------------------|-----------------------------------------------|--------|
| Same-actor personal   | `user_id=actor_key, agent_id=group_id`        | 1.0    |
| Team memories         | `user_id=__team__, agent_id=group_id`         | 1.0    |
| Other actors          | `agent_id=group_id` (exclude own + team)      | 0.5    |

---

## Deduplication

Before writing, the store searches for near-identical memories (cosine similarity ≥ 0.92).
If a match is found the write is skipped. Mem0's own internal deduplication also runs server-side.

---

## Memory Agent (planned)

A dedicated agent that replaces direct mem0 writes from the Hermes plugin.

### Per-turn processing (`POST /process-turn`)

1. Receive conversation turn (user + assistant messages, actor list, run_id)
2. Use an LLM to extract candidate facts from the turn
3. For each fact: search existing memories (same-actor scope)
4. Decide per fact: **ADD** (new) / **SKIP** (redundant, sim ≥ 0.92) / **UPDATE** (contradicts existing)
5. Write accepted facts to mem0 with `infer=False` (verbatim, LLM already extracted)
6. Determine scope (personal vs team) based on fact content

### Batch pruning (`POST /prune`)

1. Fetch all memories for a user (or all users)
2. LLM identifies: duplicates, contradictions, outdated facts
3. Delete or merge accordingly
4. Returns a pruning report

The agent is deployed as a FastAPI service on OpenShift.
The Hermes plugin routes `_bg_sync` through `MEMORY_AGENT_URL` if set, falling back to direct mem0 writes.

---

## Breaking Change Note

Migrating from the previous storage format (plain `user_id="Erwan"`) requires re-ingesting
existing memories, as the new actor-key format (`user_id="agent:flaude|user:Erwan"`) does not
match old records in search queries.
