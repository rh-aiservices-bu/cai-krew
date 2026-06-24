"""
Quick end-to-end test for the mem0 plugin storage/search flow.

Tests:
  1. Add a personal memory (infer=True, personal prompt)
  2. Add a team memory (infer=True, team prompt)
  3. Search personal, team, and other-actors
  4. Delete both memories

Usage:
  MEM0_URL=https://mem0-server-cai-crew.apps.cluster-9shz5.9shz5.sandbox4079.opentlc.com \
  python mem0_search_test.py
"""

import json
import os
import time
import httpx

# ── Config ────────────────────────────────────────────────────────────────────

URL       = os.getenv("MEM0_URL", "https://mem0-server-cai-crew.apps.cluster-9shz5.9shz5.sandbox4079.opentlc.com").rstrip("/")
USER_ID   = "test-user"
AGENT_ID  = "hermes"
ACTOR_KEY = "|".join(sorted([USER_ID, AGENT_ID]))   # "hermes|test-user"
TEAM_SCOPE = "__team__"
RUN_ID    = "mem0-search-test"

client       = httpx.Client(base_url=URL, timeout=10.0)
write_client = httpx.Client(base_url=URL, timeout=90.0)

# ── Prompts (mirrors plugin) ───────────────────────────────────────────────────

PERSONAL_PROMPT = f"""\
IDENTITY (highest priority — overrides all default naming conventions):
- The "user" role in this conversation is a person named '{USER_ID}'. \
ALWAYS use '{USER_ID}' as the subject. NEVER write 'User', 'the user', or any pronoun as a standalone subject.
- The "assistant" role in this conversation is an AI agent named '{AGENT_ID}'. \
ALWAYS use '{AGENT_ID}' when referring to the assistant.

ATTRIBUTION RULES:
- Every extracted memory MUST name its subject explicitly.
- When {USER_ID} states a personal fact: "{USER_ID} <fact>." (e.g. "{USER_ID} likes apples.")"""

TEAM_PROMPT = f"""\
TEAM MEMORY SCOPE (highest priority):
You are extracting memories for a SHARED TEAM knowledge base visible to everyone.

ONLY extract facts broadly relevant to the whole team: project decisions, technical standards,
architecture choices, shared processes, or domain knowledge multiple team members would benefit from.

DO NOT extract personal preferences or facts only relevant to the individual speaking.

ATTRIBUTION: Write "{USER_ID} noted that ..." or "{USER_ID} decided that ...". Never write 'User'.

If no team-relevant facts are present, return an empty memory list."""

# ── Helpers ───────────────────────────────────────────────────────────────────

def post(path, body, write=False):
    c = write_client if write else client
    r = c.post(path, json=body)
    r.raise_for_status()
    return r.json()

def delete(memory_id):
    r = client.delete(f"/memories/{memory_id}")
    r.raise_for_status()

def search(filters, query="test memory", top_k=5):
    body = {"query": query, "top_k": top_k}
    if filters:
        body["filters"] = filters
    r = client.post("/search", json=body)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("results", [])

def print_results(label, results):
    print(f"\n{'─'*60}")
    print(f"  {label}  ({len(results)} result(s))")
    print(f"{'─'*60}")
    if not results:
        print("  (none)")
    for m in results:
        print(f"  [{m.get('score', '?'):.3f}] {m.get('memory', m)}")
        print(f"         user_id={m.get('user_id')}  agent_id={m.get('agent_id')}")

# ── 1. Add personal memory ─────────────────────────────────────────────────────

print("\n=== STEP 1: Add personal memory ===")
personal_body = {
    "messages": [
        {"role": "user",      "content": f"My name is {USER_ID} and I love hiking in the mountains."},
        {"role": "assistant", "content": "That sounds wonderful!"},
    ],
    "user_id":  USER_ID,
    "agent_id": ACTOR_KEY,
    "run_id":   RUN_ID,
    "infer":    True,
    "prompt":   PERSONAL_PROMPT,
    "metadata": {"scope": "personal", "actors": ACTOR_KEY},
}
result = post("/memories", personal_body, write=True)
personal_ids = [r["id"] for r in result.get("results", [])]
print(f"  Stored {len(personal_ids)} personal memory(s): {personal_ids}")

# ── 2. Add team memory ─────────────────────────────────────────────────────────

print("\n=== STEP 2: Add team memory ===")
team_body = {
    "messages": [
        {"role": "user",      "content": "We have decided to use PostgreSQL with pgvector for all vector storage across the project."},
        {"role": "assistant", "content": "Noted, I'll make sure the team knows."},
    ],
    "user_id":  USER_ID,
    "agent_id": TEAM_SCOPE,
    "run_id":   RUN_ID,
    "infer":    True,
    "prompt":   TEAM_PROMPT,
    "metadata": {"scope": "team", "actors": ACTOR_KEY},
}
result = post("/memories", team_body, write=True)
team_ids = [r["id"] for r in result.get("results", [])]
print(f"  Stored {len(team_ids)} team memory(s): {team_ids}")

# ── Wait for indexing ──────────────────────────────────────────────────────────

print("\n  Waiting 3s for indexing...")
time.sleep(3)

# ── 3. Search ─────────────────────────────────────────────────────────────────

print("\n=== STEP 3: Search ===")

# _search_personal: filters by user_id + actor_key
personal_results = search(
    filters={"user_id": USER_ID, "agent_id": ACTOR_KEY},
    query="hiking mountains personal preference",
)
print_results("_search_personal", personal_results)

# _search_team: filters by agent_id=__team__
team_results = search(
    filters={"agent_id": TEAM_SCOPE},
    query="postgresql pgvector vector storage",
)
print_results("_search_team", team_results)

# _search_other_actors: no filters (global), then exclude own actor_key and team
try:
    other_raw = search(filters={}, query="test memory", top_k=9)
    other_results = [
        m for m in other_raw
        if not (m.get("user_id") == USER_ID and m.get("agent_id") == ACTOR_KEY)
        and m.get("agent_id") != TEAM_SCOPE
    ][:3]
    print_results("_search_other_actors", other_results)
except httpx.HTTPStatusError as e:
    print(f"\n  _search_other_actors → HTTP {e.response.status_code}: {e.response.text}")

# ── 4. Delete ─────────────────────────────────────────────────────────────────

print("\n=== STEP 4: Delete memories ===")
all_ids = personal_ids + team_ids
if not all_ids:
    print("  No memories to delete.")
else:
    for mid in all_ids:
        delete(mid)
        print(f"  Deleted {mid}")

print("\n=== Done ===\n")
