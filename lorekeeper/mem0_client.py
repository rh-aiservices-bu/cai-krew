"""Thin REST client for the mem0 server."""
from __future__ import annotations

import logging
from typing import Dict, List

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_TEAM_SCOPE = "__team__"

# The server caps GET /memories (no filters) at this many rows.
# See ALL_MEMORIES_LIMIT in mem0/server/main.py.
_LIST_ALL_LIMIT = 1000


class Mem0Client:
    def __init__(self, base_url: str) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=_TIMEOUT)

    def discover_actor_keys(self) -> List[str]:
        """Return every distinct personal actor key present in the memory store.

        GET /memories with no filters lists all memories (up to _LIST_ALL_LIMIT).
        This is permitted when AUTH_DISABLED=true (our OpenShift deployment).
        Actor keys are the agent_id values on personal memories — everything
        except __team__.
        """
        r = self._client.get("/memories", params={"top_k": _LIST_ALL_LIMIT})
        r.raise_for_status()
        data = r.json()
        memories = data if isinstance(data, list) else data.get("results", [])
        keys = {
            m["agent_id"]
            for m in memories
            if m.get("agent_id") and m["agent_id"] != _TEAM_SCOPE
        }
        return sorted(keys)

    def get_personal_memories(self, actor_key: str) -> List[Dict]:
        """Fetch all personal memories for an actor key.

        In the plugin's storage model, personal memories are stored with
        agent_id = actor_key (e.g. "hermes|test-user"), so we filter by that.
        top_k must be set explicitly — the SDK default is 20.
        """
        r = self._client.get("/memories", params={"agent_id": actor_key, "top_k": _LIST_ALL_LIMIT})
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("results", [])

    def get_team_memories(self) -> List[Dict]:
        """Fetch all team-scoped memories (agent_id == __team__).

        top_k must be set explicitly — the SDK default is 20.
        """
        r = self._client.get("/memories", params={"agent_id": _TEAM_SCOPE, "top_k": _LIST_ALL_LIMIT})
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("results", [])

    # ── v2 stubs ──────────────────────────────────────────────────────────────

    def delete_memory(self, memory_id: str) -> None:
        raise NotImplementedError("delete not enabled in v1 (report-only mode)")

    def update_memory(self, memory_id: str, text: str) -> Dict:
        """PUT /memories/{id} — used for MERGE: update in-place instead of delete+add."""
        raise NotImplementedError("update not enabled in v1 (report-only mode)")

    def close(self) -> None:
        self._client.close()
