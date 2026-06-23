"""Mem0 self-hosted memory provider for Hermes Agent.

Connects directly to a self-hosted Mem0 server via REST.
No SDK, no cloud account required.

Config (env vars or ~/.hermes/.env):
  MEM0_URL                 Base URL of your Mem0 server (required)
  MEM0_USER_ID             User actor ID — falls back to Hermes's user_id kwarg (default: hermes-user)
  MEM0_AGENT_ID            Agent actor ID — falls back to hermes_{agent_identity} (default: hermes)
  MEM0_CUSTOM_INSTRUCTIONS Optional instructions injected into Mem0's fact extraction prompt

Scoping:
  Personal memories are keyed by a composite of all actor IDs (sorted, joined by |).
  Team memories use user_id="__team__" and are always included in search results.
  Other actors' memories are surfaced at lower weight (0.5) for natural knowledge sharing.

Search tiers (highest to lowest weight):
  1. Same actor set  — weight 1.0
  2. Team scope      — weight 1.0
  3. Other actors    — weight 0.5
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0
_CB_THRESHOLD = 5
_CB_COOLDOWN  = 120.0
_TEAM_USER_ID = "__team__"
_DEDUP_THRESHOLD = 0.92
_OTHER_ACTOR_WEIGHT = 0.5


def _actor_key(actor_ids: list[str]) -> str:
    """Stable composite key from a list of actor IDs."""
    return "|".join(sorted(actor_ids))


class Mem0OssProvider(MemoryProvider):

    @property
    def name(self) -> str:
        return "mem0_oss"

    def __init__(self) -> None:
        self._url: str = ""
        self._user_id: str = "hermes-user"
        self._agent_id: str = "hermes"
        self._actor_key: str = ""        # composite key for personal memories
        self._run_id: str = ""
        self._custom_instructions: Optional[str] = None
        self._client: Optional[httpx.Client] = None
        self._lock = threading.Lock()
        self._prefetch_cache: str = ""
        self._cb_failures: int = 0
        self._cb_tripped_at: float = 0.0

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        return bool(os.getenv("MEM0_URL"))

    def initialize(self, session_id: str, **kwargs) -> None:
        self._url      = os.getenv("MEM0_URL", "").rstrip("/")
        self._user_id  = os.getenv("MEM0_USER_ID") or kwargs.get("user_id") or "hermes-user"
        if os.getenv("MEM0_AGENT_ID"):
            self._agent_id = os.getenv("MEM0_AGENT_ID")
        elif kwargs.get("agent_identity"):
            self._agent_id = str(kwargs["agent_identity"])
        else:
            self._agent_id = "hermes"
        self._actor_key   = _actor_key([self._user_id, self._agent_id])
        self._run_id      = session_id
        self._custom_instructions = os.getenv("MEM0_CUSTOM_INSTRUCTIONS") or None
        self._client      = httpx.Client(base_url=self._url, timeout=_TIMEOUT)
        logger.info(
            "mem0_oss: connected to %s (actors=%s, run=%s)",
            self._url, self._actor_key, self._run_id,
        )

    def shutdown(self) -> None:
        if self._client:
            self._client.close()

    # ── Circuit breaker ──────────────────────────────────────────────────────

    def _cb_ok(self) -> bool:
        if self._cb_failures >= _CB_THRESHOLD:
            if time.time() - self._cb_tripped_at < _CB_COOLDOWN:
                return False
            self._cb_failures = 0
        return True

    def _request(self, method: str, path: str, **kwargs) -> Any:
        if not self._cb_ok():
            raise RuntimeError("mem0 circuit breaker open")
        try:
            r = self._client.request(method, path, **kwargs)
            r.raise_for_status()
            self._cb_failures = 0
            return r.json()
        except Exception:
            self._cb_failures += 1
            if self._cb_failures >= _CB_THRESHOLD:
                self._cb_tripped_at = time.time()
                logger.warning("mem0_oss: circuit breaker tripped after %d failures", _CB_THRESHOLD)
            raise

    # ── Memory helpers ───────────────────────────────────────────────────────

    def _search_personal(self, query: str, top_k: int = 5) -> List[Dict]:
        """Memories from the exact same actor set — highest relevance."""
        body: Dict[str, Any] = {
            "query":   query,
            "filters": {"user_id": self._actor_key},
            "top_k":   top_k,
        }
        data = self._request("POST", "/search", json=body)
        return data if isinstance(data, list) else data.get("results", [])

    def _search_team(self, query: str, top_k: int = 5) -> List[Dict]:
        """Team-scoped memories — always included, same weight as personal."""
        body: Dict[str, Any] = {
            "query":   query,
            "filters": {"user_id": _TEAM_USER_ID},
            "top_k":   top_k,
        }
        data = self._request("POST", "/search", json=body)
        return data if isinstance(data, list) else data.get("results", [])

    def _search_other_actors(self, query: str, top_k: int = 3) -> List[Dict]:
        """Memories from other actor pairs — surfaced at lower weight."""
        data = self._request("POST", "/search", json={"query": query, "top_k": top_k * 3})
        results = data if isinstance(data, list) else data.get("results", [])
        return [
            m for m in results
            if m.get("user_id") not in (self._actor_key, _TEAM_USER_ID)
        ][:top_k]

    def _add(self, messages: List[Dict], scope: str = "personal", infer: bool = True) -> None:
        user_id = _TEAM_USER_ID if scope == "team" else self._actor_key
        body: Dict[str, Any] = {
            "messages": messages,
            "user_id":  user_id,
            "agent_id": self._agent_id,
            "run_id":   self._run_id,
            "infer":    infer,
            "metadata": {
                "scope":      scope,
                "actors":     self._actor_key,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        if self._custom_instructions:
            body["custom_instructions"] = self._custom_instructions
        self._request("POST", "/memories", json=body)

    def _get_all(self) -> List[Dict]:
        data = self._request("GET", "/memories", params={"user_id": self._actor_key})
        return data if isinstance(data, list) else data.get("results", [])

    def _already_known(self, content: str) -> bool:
        """Skip save if Mem0 already has a highly similar memory for these actors."""
        try:
            hits = self._search_personal(content, top_k=3)
            return any(h.get("score", 0) >= _DEDUP_THRESHOLD for h in hits)
        except Exception:
            return False

    # ── Per-turn hooks ───────────────────────────────────────────────────────

    def system_prompt_block(self) -> str:
        return (
            f"You have persistent long-term memory via mem0 (actors: {self._actor_key}). "
            "Use `mem0_search` to recall relevant facts before answering, "
            "`mem0_profile` to review everything stored for the current actor pair, "
            "and `mem0_conclude` to save a specific fact verbatim.\n"
            "Search results are ranked across three tiers:\n"
            "- 'Your memories' — facts from this exact conversation pair (highest confidence)\n"
            "- 'Team memories' — shared team knowledge\n"
            "- 'Other memories' — facts from other actor pairs, prefixed with [actors] "
            "(lower weight — attribute them, e.g. 'bob mentioned he prefers YAML over JSON')"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        with self._lock:
            result, self._prefetch_cache = self._prefetch_cache, ""
        return result

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        threading.Thread(target=self._bg_prefetch, args=(query,), daemon=True).start()

    def _bg_prefetch(self, query: str) -> None:
        try:
            personal = self._search_personal(query, top_k=5)
            team     = self._search_team(query, top_k=3)
            other    = self._search_other_actors(query, top_k=3)
            sections = []
            if personal:
                lines = "\n".join(f"- {m.get('memory', m)}" for m in personal)
                sections.append(f"## Your memories:\n{lines}")
            if team:
                lines = "\n".join(f"- {m.get('memory', m)}" for m in team)
                sections.append(f"## Team memories:\n{lines}")
            if other:
                lines = "\n".join(
                    f"- [{m.get('user_id', '?')}] {m.get('memory', m)}" for m in other
                )
                sections.append(f"## Other memories (lower confidence):\n{lines}")
            if sections:
                with self._lock:
                    self._prefetch_cache = "\n\n".join(sections)
        except Exception as exc:
            logger.debug("mem0_oss prefetch failed: %s", exc)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if not user_content:
            return
        threading.Thread(
            target=self._bg_sync,
            args=(user_content, assistant_content),
            daemon=True,
        ).start()

    def _bg_sync(self, user_msg: str, asst_msg: str) -> None:
        try:
            if self._already_known(user_msg):
                logger.debug("mem0_oss: skipping duplicate memory")
                return
            self._add([
                {"role": "user",      "content": user_msg},
                {"role": "assistant", "content": asst_msg},
            ])
        except Exception as exc:
            logger.debug("mem0_oss sync failed: %s", exc)

    # ── Tools ────────────────────────────────────────────────────────────────

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "mem0_search",
                "description": (
                    "Search long-term memory for facts relevant to a query. "
                    "Returns three tiers: your memories (same actor pair), "
                    "team memories (shared), and other actors' memories (lower weight)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to search for"},
                        "top_k": {"type": "integer", "description": "Max results per tier (default 5)"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "mem0_profile",
                "description": "Retrieve all stored memories for the current actor pair.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "mem0_conclude",
                "description": (
                    "Store a specific fact verbatim (skips LLM extraction). "
                    "Use scope='team' to share with the whole team."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "fact":  {"type": "string", "description": "The fact to store"},
                        "scope": {
                            "type": "string",
                            "enum": ["personal", "team"],
                            "description": "personal (default) or team",
                        },
                    },
                    "required": ["fact"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            if tool_name == "mem0_search":
                top_k    = args.get("top_k", 5)
                personal = self._search_personal(args["query"], top_k=top_k)
                team     = self._search_team(args["query"], top_k=top_k)
                other    = self._search_other_actors(args["query"], top_k=3)
                if not personal and not team and not other:
                    return json.dumps({"result": "No relevant memories found."})
                result: Dict[str, Any] = {}
                if personal:
                    result["your_memories"] = [m.get("memory", str(m)) for m in personal]
                if team:
                    result["team_memories"] = [m.get("memory", str(m)) for m in team]
                if other:
                    result["other_memories"] = [
                        {"actors": m.get("user_id", "?"), "memory": m.get("memory", str(m))}
                        for m in other
                    ]
                return json.dumps(result)

            if tool_name == "mem0_profile":
                memories = self._get_all()
                if not memories:
                    return json.dumps({"result": "No memories stored yet."})
                return json.dumps({"memories": [m.get("memory", str(m)) for m in memories]})

            if tool_name == "mem0_conclude":
                scope = args.get("scope", "personal")
                self._add(
                    [{"role": "user", "content": args["fact"]}],
                    scope=scope,
                    infer=False,
                )
                return json.dumps({"result": f"Memory stored ({scope})."})

        except RuntimeError:
            return json.dumps({"error": "Memory service temporarily unavailable."})
        except Exception as exc:
            logger.warning("mem0_oss tool error: %s", exc)
            return json.dumps({"error": str(exc)})

        raise NotImplementedError(tool_name)


def register():
    return Mem0OssProvider
