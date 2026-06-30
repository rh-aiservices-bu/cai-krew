"""mem0_client — framework-agnostic client for a self-hosted mem0 server.

Handles all mem0 business logic: HTTP transport, circuit breaker, three-tier
search (personal / team / other), team-vs-personal routing, extraction prompts,
and deduplication.

Usage::

    from mem0_client import Mem0Client

    client = Mem0Client(
        url="https://mem0-server.example.com",
        user_id="Erwan",
        agent_id="hermes",
        run_id="session-abc",
    )

    client.sync_turn("I love hiking.", "Great to know!")
    results = client.search_personal("outdoor activities")

Config (all optional — can also be passed as constructor args):
    MEM0_URL                  Base URL of mem0 server
    MEM0_USER_ID              Human user ID
    MEM0_AGENT_ID             Agent/tool ID
    MEM0_CUSTOM_INSTRUCTIONS  Extra instructions appended to extraction prompts

Storage model:
    user_id  = the human user (e.g. "Erwan")
    agent_id = scope:
                 personal → composite actor key (e.g. "Erwan|hermes")
                 team     → "__team__"
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(filename: str, **kwargs) -> str:
    return (_PROMPTS_DIR / filename).read_text().format(**kwargs)

logger = logging.getLogger(__name__)

TEAM_SCOPE       = "__team__"
TIMEOUT          = 10.0    # read operations
WRITE_TIMEOUT    = 90.0    # write operations — LLM extraction can take 30–60 s
CB_THRESHOLD     = 5
CB_COOLDOWN      = 120.0
DEDUP_THRESHOLD  = 0.92


def make_actor_key(actor_ids: List[str]) -> str:
    """Stable composite key from a list of actor IDs (e.g. ['Erwan', 'hermes'])."""
    return "|".join(sorted(actor_ids))


class Mem0Client:
    """All mem0 business logic, framework-agnostic."""

    def __init__(
        self,
        url: str,
        user_id: str,
        agent_id: str,
        run_id: str,
        extra_instructions: str = "",
    ) -> None:
        self.url        = url.rstrip("/")
        self.user_id    = user_id.replace(" ", "_")
        self.agent_id   = agent_id.replace(" ", "_")
        self.actor_key  = make_actor_key([self.user_id, self.agent_id])
        self.run_id     = run_id
        self.extra_instructions = extra_instructions

        self._client       = httpx.Client(base_url=self.url, timeout=TIMEOUT)
        self._write_client = httpx.Client(base_url=self.url, timeout=WRITE_TIMEOUT)
        self._cb_failures  = 0
        self._cb_tripped_at = 0.0

        logger.info(
            "mem0_client: connected to %s (actors=%s, run=%s)",
            self.url, self.actor_key, self.run_id,
        )

    def close(self) -> None:
        self._client.close()
        self._write_client.close()

    # ── Circuit breaker ───────────────────────────────────────────────────────

    def _cb_ok(self) -> bool:
        if self._cb_failures >= CB_THRESHOLD:
            if time.time() - self._cb_tripped_at < CB_COOLDOWN:
                return False
            self._cb_failures = 0
        return True

    def _request(self, method: str, path: str, *, _write: bool = False, **kwargs) -> Any:
        if not self._cb_ok():
            raise RuntimeError("mem0 circuit breaker open")
        client = self._write_client if _write else self._client
        try:
            r = client.request(method, path, **kwargs)
            r.raise_for_status()
            self._cb_failures = 0
            return r.json()
        except Exception:
            self._cb_failures += 1
            if self._cb_failures >= CB_THRESHOLD:
                self._cb_tripped_at = time.time()
                logger.warning("mem0_client: circuit breaker tripped after %d failures", CB_THRESHOLD)
            raise

    # ── Search ────────────────────────────────────────────────────────────────

    def search_personal(self, query: str, top_k: int = 5) -> List[Dict]:
        """Memories for this exact user+agent pair."""
        data = self._request("POST", "/search", json={
            "query":   query,
            "filters": {"user_id": self.user_id, "agent_id": self.actor_key},
            "top_k":   top_k,
        })
        return data if isinstance(data, list) else data.get("results", [])

    def search_team(self, query: str, top_k: int = 5) -> List[Dict]:
        """Team-scoped memories shared across all actors."""
        data = self._request("POST", "/search", json={
            "query":   query,
            "filters": {"agent_id": TEAM_SCOPE},
            "top_k":   top_k,
        })
        return data if isinstance(data, list) else data.get("results", [])

    def search_other_actors(self, query: str, top_k: int = 3) -> List[Dict]:
        """Personal memories from other user/agent pairs (global, filtered in Python)."""
        try:
            data = self._request("POST", "/search", json={
                "query":  query,
                "top_k":  top_k * 3,
            })
            results = data if isinstance(data, list) else data.get("results", [])
            return [
                m for m in results
                if not (
                    m.get("user_id") == self.user_id and m.get("agent_id") == self.actor_key
                ) and m.get("agent_id") != TEAM_SCOPE
            ][:top_k]
        except Exception as exc:
            logger.debug("mem0_client search_other_actors failed (non-fatal): %s", exc)
            return []

    def format_search_results(
        self,
        personal: List[Dict],
        team: List[Dict],
        other: List[Dict],
    ) -> str:
        """Format three-tier search results into a human-readable string."""
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
        return "\n\n".join(sections)

    def search_all(self, query: str, top_k: int = 5) -> str:
        """Run all three search tiers and return formatted results string."""
        personal = self.search_personal(query, top_k=top_k)
        team     = self.search_team(query, top_k=min(top_k, 3))
        other    = self.search_other_actors(query, top_k=3)
        return self.format_search_results(personal, team, other)

    # ── Storage ───────────────────────────────────────────────────────────────

    def add(
        self,
        messages: List[Dict],
        scope: str = "personal",
        infer: bool = True,
        prompt: Optional[str] = None,
    ) -> bool:
        """Store memories. Returns True if at least one memory was saved."""
        body: Dict[str, Any] = {
            "messages": messages,
            "user_id":  self.user_id,
            "agent_id": TEAM_SCOPE if scope == "team" else self.actor_key,
            "run_id":   self.run_id,
            "infer":    infer,
            "metadata": {
                "scope":      scope,
                "actors":     self.actor_key,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        if infer:
            body["prompt"] = prompt or self.extraction_prompt()
        data = self._request("POST", "/memories", json=body, _write=True)
        results = data.get("results", []) if isinstance(data, dict) else (data or [])
        return bool(results)

    def sync_turn(self, user_msg: str, asst_msg: str) -> None:
        """Store a conversation turn — team first, fall through to personal."""
        messages = [
            {"role": "user",      "content": user_msg},
            {"role": "assistant", "content": asst_msg},
        ]
        try:
            stored_as_team = self.add(messages, scope="team", prompt=self.team_extraction_prompt())
        except Exception as exc:
            logger.debug("mem0_client team sync failed: %s", exc)
            stored_as_team = False

        if stored_as_team:
            logger.debug("mem0_client: stored as team memory, skipping personal")
            return

        try:
            if not self.already_known(user_msg):
                self.add(messages, scope="personal")
            else:
                logger.debug("mem0_client: skipping duplicate personal memory")
        except Exception as exc:
            logger.debug("mem0_client personal sync failed: %s", exc)

    def get_all(self) -> List[Dict]:
        """Get all personal memories for this actor pair."""
        data = self._request("GET", "/memories", params={
            "user_id":  self.user_id,
            "agent_id": self.actor_key,
        })
        return data if isinstance(data, list) else data.get("results", [])

    def delete(self, memory_id: str) -> None:
        self._request("DELETE", f"/memories/{memory_id}")

    def already_known(self, content: str) -> bool:
        """Return True if a highly similar memory already exists for this actor pair."""
        try:
            hits = self.search_personal(content, top_k=3)
            return any(h.get("score", 0) >= DEDUP_THRESHOLD for h in hits)
        except Exception:
            return False

    # ── Prompts ───────────────────────────────────────────────────────────────

    def extraction_prompt(self) -> str:
        """Extraction prompt for personal memories — loaded from prompts/extraction.txt."""
        prompt = _load_prompt("extraction.txt", user_id=self.user_id, agent_id=self.agent_id)
        if self.extra_instructions:
            prompt += f"\n\nADDITIONAL INSTRUCTIONS:\n{self.extra_instructions}"
        return prompt

    def team_extraction_prompt(self) -> str:
        """Extraction prompt for team memories — loaded from prompts/team_extraction.txt."""
        return _load_prompt("team_extraction.txt", user_id=self.user_id)

    # ── Tools ─────────────────────────────────────────────────────────────────

    def get_tool_schemas(self) -> List[Dict]:
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
            # mem0_conclude: store a specific fact verbatim (infer=False), bypassing LLM extraction.
            # {
            #     "name": "mem0_conclude",
            #     "description": (
            #         "Store a specific fact verbatim (skips LLM extraction). "
            #         "Use scope='team' to share with the whole team."
            #     ),
            #     "parameters": {
            #         "type": "object",
            #         "properties": {
            #             "fact":  {"type": "string", "description": "The fact to store"},
            #             "scope": {
            #                 "type": "string",
            #                 "enum": ["personal", "team"],
            #                 "description": "personal (default) or team",
            #             },
            #         },
            #         "required": ["fact"],
            #     },
            # },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any]) -> str:
        import json
        try:
            if tool_name == "mem0_search":
                top_k    = args.get("top_k", 5)
                personal = self.search_personal(args["query"], top_k=top_k)
                team     = self.search_team(args["query"], top_k=top_k)
                other    = self.search_other_actors(args["query"], top_k=3)
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
                memories = self.get_all()
                if not memories:
                    return json.dumps({"result": "No memories stored yet."})
                return json.dumps({"memories": [m.get("memory", str(m)) for m in memories]})

            # if tool_name == "mem0_conclude":
            #     scope = args.get("scope", "personal")
            #     self.add(
            #         [{"role": "user", "content": args["fact"]}],
            #         scope=scope,
            #         infer=False,
            #     )
            #     return json.dumps({"result": f"Memory stored ({scope})."})

        except RuntimeError:
            return json.dumps({"error": "Memory service temporarily unavailable."})
        except Exception as exc:
            logger.warning("mem0_client tool error: %s", exc)
            return json.dumps({"error": str(exc)})

        raise NotImplementedError(tool_name)

    # ── Convenience constructor from env vars ─────────────────────────────────

    @classmethod
    def from_env(
        cls,
        *,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: str = "",
    ) -> "Mem0Client":
        """Construct a Mem0Client from environment variables with optional overrides."""
        return cls(
            url=os.getenv("MEM0_URL", "").rstrip("/"),
            user_id=os.getenv("MEM0_USER_ID") or user_id or "user",
            agent_id=os.getenv("MEM0_AGENT_ID") or agent_id or "assistant",
            run_id=run_id,
            extra_instructions=os.getenv("MEM0_CUSTOM_INSTRUCTIONS") or "",
        )
