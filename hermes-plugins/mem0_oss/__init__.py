"""Mem0 self-hosted memory provider for Hermes Agent.

Connects directly to a self-hosted Mem0 server via REST.
No SDK, no cloud account required.

Config (env vars or ~/.hermes/.env):
  MEM0_URL                    Base URL of your Mem0 server (required)
  MEM0_USER_ID                User actor ID — falls back to Hermes's user_id kwarg (default: hermes-user)
  MEM0_AGENT_ID               Agent actor ID — falls back to agent_identity kwarg (default: hermes)
  MEM0_CUSTOM_INSTRUCTIONS    Optional extra instructions appended to the auto-generated extraction prompt

Storage model:
  user_id  = the human user (e.g. "Erwan") — who the memory belongs to
  agent_id = the scope:
               personal → composite actor key  (e.g. "agent:hermes|user:Erwan")
               team     → "__team__"

Search tiers (highest to lowest weight):
  1. Personal (same actor set)  — agent_id=actor_key,  weight 1.0
  2. Team                       — agent_id=__team__,   weight 1.0
  3. Other actors               — global, exclude own + team, weight 0.5
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

_TIMEOUT       = 10.0   # read operations (search, get)
_WRITE_TIMEOUT = 90.0   # write operations (add) — LLM extraction can take 30–60 s
_CB_THRESHOLD = 5
_CB_COOLDOWN  = 120.0
_TEAM_SCOPE = "__team__"
_DEDUP_THRESHOLD = 0.92


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
        self._client: Optional[httpx.Client] = None
        self._write_client: Optional[httpx.Client] = None
        self._lock = threading.Lock()
        self._prefetch_cache: str = ""
        self._extra_instructions: str = ""
        self._cb_failures: int = 0
        self._cb_tripped_at: float = 0.0

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        return bool(os.getenv("MEM0_URL"))

    def initialize(self, session_id: str, **kwargs) -> None:
        self._url      = os.getenv("MEM0_URL", "").rstrip("/")
        # Prefer a clean human-readable name over raw user_id, but only if it's actually useful.
        _raw_name = kwargs.get("user_name") or ""
        _raw_uid  = kwargs.get("user_id") or ""
        # Only use user_name if it's non-empty, not just a Slack handle (@user), and looks like a real name.
        if _raw_name and _raw_name.strip() and not _raw_name.startswith("@"):
            self._user_id = _raw_name.strip().replace(" ", "_")
        elif _raw_uid:
            self._user_id = _raw_uid.replace(" ", "_")
        else:
            self._user_id = (os.getenv("MEM0_USER_ID") or "hermes-user").replace(" ", "_")
        if os.getenv("MEM0_AGENT_ID"):
            self._agent_id = os.getenv("MEM0_AGENT_ID")
        elif kwargs.get("agent_identity"):
            self._agent_id = str(kwargs["agent_identity"])
        else:
            self._agent_id = "hermes"
        self._actor_key   = _actor_key([self._user_id, self._agent_id])
        self._run_id             = session_id
        self._extra_instructions = os.getenv("MEM0_CUSTOM_INSTRUCTIONS") or ""
        self._client             = httpx.Client(base_url=self._url, timeout=_TIMEOUT)
        self._write_client       = httpx.Client(base_url=self._url, timeout=_WRITE_TIMEOUT)
        logger.info(
            "mem0_oss: connected to %s (actors=%s, run=%s)",
            self._url, self._actor_key, self._run_id,
        )

    def shutdown(self) -> None:
        if self._client:
            self._client.close()
        if self._write_client:
            self._write_client.close()

    # ── Circuit breaker ──────────────────────────────────────────────────────

    def _cb_ok(self) -> bool:
        if self._cb_failures >= _CB_THRESHOLD:
            if time.time() - self._cb_tripped_at < _CB_COOLDOWN:
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
            if self._cb_failures >= _CB_THRESHOLD:
                self._cb_tripped_at = time.time()
                logger.warning("mem0_oss: circuit breaker tripped after %d failures", _CB_THRESHOLD)
            raise

    # ── Memory helpers ───────────────────────────────────────────────────────

    def _search_personal(self, query: str, top_k: int = 5) -> List[Dict]:
        """Memories for this exact user+actor-set pair."""
        data = self._request("POST", "/search", json={
            "query":   query,
            "filters": {"user_id": self._user_id, "agent_id": self._actor_key},
            "top_k":   top_k,
        })
        return data if isinstance(data, list) else data.get("results", [])

    def _search_team(self, query: str, top_k: int = 5) -> List[Dict]:
        """Team-scoped memories — shared across all actors."""
        data = self._request("POST", "/search", json={
            "query":   query,
            "filters": {"agent_id": _TEAM_SCOPE},
            "top_k":   top_k,
        })
        return data if isinstance(data, list) else data.get("results", [])

    def _search_other_actors(self, query: str, top_k: int = 3) -> List[Dict]:
        """Personal memories from other users — surfaced at lower weight."""
        try:
            data = self._request("POST", "/search", json={
                "query":  query,
                "top_k":  top_k * 3,
            })
        except Exception:
            return []
        results = data if isinstance(data, list) else data.get("results", [])
        return [
            m for m in results
            if not (
                m.get("user_id") == self._user_id and m.get("agent_id") == self._actor_key
            ) and m.get("agent_id") != _TEAM_SCOPE
        ][:top_k]

    def _extraction_prompt(self) -> str:
        """Custom instructions for personal memory extraction — uses real actor names."""
        u = self._user_id
        a = self._agent_id
        prompt = f"""\
IDENTITY (highest priority — overrides all default naming conventions):
- The "user" role in this conversation is a person named {u!r}. \
ALWAYS use '{u}' as the subject. NEVER write 'User', 'the user', or any pronoun as a standalone subject.
- The "assistant" role in this conversation is an AI agent named {a!r}. \
ALWAYS use '{a}' when referring to the assistant. NEVER write 'the assistant' or 'Assistant'.

ATTRIBUTION RULES:
- Every extracted memory MUST name its subject explicitly: start with '{u}' or '{a}', never 'User' or 'they'.
- When {u} states a personal fact, preference, or experience: "{u} <fact>." (e.g. "{u} likes apples.")
- When {a} makes a recommendation or provides information to {u}: \
"{a} recommended ... to {u}" or "{u} was told by {a} that ..."
- When {u} explicitly shares something with {a} in conversation: \
"{u} told {a} that ..." is preferred.

EXAMPLES:
  BAD  → "User likes hiking."          GOOD → "{u} likes hiking."
  BAD  → "The user prefers tea."       GOOD → "{u} prefers tea."
  BAD  → "User was recommended Python." GOOD → "{a} recommended Python to {u}."
  BAD  → "They enjoy reading."         GOOD → "{u} enjoys reading.\""""
        if self._extra_instructions:
            prompt += f"\n\nADDITIONAL INSTRUCTIONS:\n{self._extra_instructions}"
        return prompt

    def _team_extraction_prompt(self) -> str:
        """Custom instructions for team memory extraction."""
        u = self._user_id
        return f"""\
TEAM MEMORY SCOPE (highest priority):
You are extracting memories for a SHARED TEAM knowledge base visible to everyone.

ONLY extract facts that are broadly relevant to the whole team, such as:
- Project or product decisions, requirements, priorities, or milestones
- Technical standards, architecture choices, tooling, or conventions
- Company or team processes, policies, workflows, or resources
- Domain knowledge or context that multiple team members would benefit from knowing
- Decisions or agreements reached in this conversation that affect the team

DO NOT extract:
- Personal preferences, habits, or individual experiences (e.g. "{u} likes apples" → skip)
- Facts that are only relevant to the person speaking
- Generic small talk or transient conversational details

ATTRIBUTION: When a fact was contributed by {u!r}, write "{u} noted that ..." \
or "{u} decided that ...". Do not write 'User' or 'the user'.

If no team-relevant facts are present in this conversation, return an empty memory list. \
It is correct and expected to return nothing most of the time."""

    def _add(
        self,
        messages: List[Dict],
        scope: str = "personal",
        infer: bool = True,
        prompt: Optional[str] = None,
    ) -> bool:
        """Store memories. Returns True if at least one memory was saved."""
        body: Dict[str, Any] = {
            "messages": messages,
            "user_id":  self._user_id,
            "agent_id": _TEAM_SCOPE if scope == "team" else self._actor_key,
            "run_id":   self._run_id,
            "infer":    infer,
            "metadata": {
                "scope":      scope,
                "actors":     self._actor_key,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        if infer:
            body["prompt"] = prompt or self._extraction_prompt()
        try:
            data = self._request("POST", "/memories", json=body, _write=True)
            results = data.get("results", []) if isinstance(data, dict) else (data or [])
            logger.debug("mem0_oss._add(%s): stored %d memories, response=%s",
                         scope, len(results), json.dumps(results)[:200])
            return bool(results)
        except Exception as e:
            logger.warning("mem0_oss._add(%s) FAILED: %s", scope, e)
            raise

    def _get_all(self) -> List[Dict]:
        data = self._request("GET", "/memories", params={
            "user_id":  self._user_id,
            "agent_id": self._actor_key,
        })
        return data if isinstance(data, list) else data.get("results", [])

    def _delete(self, memory_id: str) -> None:
        self._request("DELETE", f"/memories/{memory_id}")

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
        logger.debug("mem0_oss.prefetch(query=%r, session=%r) — cache=%s",
                     query, session_id, bool(self._prefetch_cache))
        with self._lock:
            result, self._prefetch_cache = self._prefetch_cache, ""
        if result and result.strip():
            logger.debug("mem0_oss.prefetch returning cached (%d chars)", len(result))
            return result
        # Turn 1 fallback: cache is empty because nothing was queued before the
        # first message. Run searches inline and return directly so turn 1 gets context.
        try:
            sections = []
            personal = self._search_personal(query, top_k=5)
            logger.debug("mem0_oss.prefetch: personal search returned %d results", len(personal))
            if personal:
                lines = "\n".join(f"- {m.get('memory', m)}" for m in personal)
                sections.append(f"## Your memories:\n{lines}")
            team = self._search_team(query, top_k=3)
            logger.debug("mem0_oss.prefetch: team search returned %d results", len(team))
            if team:
                lines = "\n".join(f"- {m.get('memory', m)}" for m in team)
                sections.append(f"## Team memories:\n{lines}")
            other = self._search_other_actors(query, top_k=3)
            logger.debug("mem0_oss.prefetch: other actors search returned %d results", len(other))
            if other:
                lines = "\n".join(
                    f"- [{m.get('user_id', '?')}] {m.get('memory', m)}" for m in other
                )
                sections.append(f"## Other memories (lower confidence):\n{lines}")
            if sections:
                result = "\n\n".join(sections)
                logger.debug("mem0_oss.prefetch returning %d chars", len(result))
                return result
        except Exception as exc:
            logger.error("mem0_oss.prefetch fallback FAILED: %s", exc, exc_info=True)
        logger.debug("mem0_oss.prefetch returning empty string")
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        logger.debug("mem0_oss.queue_prefetch(query=%r, session=%r)", query, session_id)
        threading.Thread(target=self._bg_prefetch, args=(query,), daemon=True).start()

    def _bg_prefetch(self, query: str) -> None:
        try:
            logger.debug("mem0_oss._bg_prefetch(query=%r) starting", query)
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
                result = "\n\n".join(sections)
                with self._lock:
                    self._prefetch_cache = result
                logger.debug("mem0_oss._bg_prefetch: cached %d chars", len(result))
            else:
                logger.debug("mem0_oss._bg_prefetch: no results for %r", query)
        except Exception as exc:
            logger.error("mem0_oss._bg_prefetch FAILED: %s", exc, exc_info=True)

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
        logger.debug("mem0_oss.sync_turn: scheduling background sync (user=%d chars, asst=%d chars)",
                     len(user_content), len(assistant_content))
        threading.Thread(
            target=self._bg_sync,
            args=(user_content, assistant_content),
            daemon=True,
        ).start()

    def _bg_sync(self, user_msg: str, asst_msg: str) -> None:
        messages = [
            {"role": "user",      "content": user_msg},
            {"role": "assistant", "content": asst_msg},
        ]

        # Try team extraction first. If something team-relevant is found and
        # stored, skip personal storage — the fact belongs to the team, not the
        # individual. Only fall through to personal if the team LLM returns empty
        # or if the team call fails (fail-safe: always store something).
        try:
            logger.debug("mem0_oss._bg_sync: attempting team extraction")
            stored_as_team = self._add(
                messages, scope="team", prompt=self._team_extraction_prompt()
            )
        except Exception as exc:
            logger.warning("mem0_oss team sync FAILED: %s", exc, exc_info=True)
            stored_as_team = False

        if stored_as_team:
            logger.debug("mem0_oss: stored as team memory, skipping personal")
            return

        # Nothing team-relevant — store as personal
        try:
            logger.debug("mem0_oss._bg_sync: team empty, attempting personal extraction")
            if not self._already_known(user_msg):
                self._add(messages, scope="personal")
            else:
                logger.debug("mem0_oss: skipping duplicate personal memory")
        except Exception as exc:
            logger.warning("mem0_oss personal sync FAILED: %s", exc, exc_info=True)

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
                        "query": {
                            "type": "string",
                            "description": "What to search for",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Max results per tier (default 5)",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "mem0_profile",
                "description": "Retrieve all stored memories for the current actor pair.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            # {
            #     "name": "mem0_conclude",
            #     "description": (
            #         "Store a specific fact verbatim (skips LLM extraction). "
            #         "Use scope='team' to share with the whole team."
            #     ),
            #     "parameters": {
            #         "type": "object",
            #         "properties": {
            #             "fact": {
            #                 "type": "string",
            #                 "description": "The fact to store",
            #             },
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

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "mem0_search":
            try:
                top_k = args.get("top_k", 5)
                personal = self._search_personal(args["query"], top_k=top_k)
                team = self._search_team(args["query"], top_k=top_k)
                other = self._search_other_actors(args["query"], top_k=3)
                if not personal and not team and not other:
                    return json.dumps({"result": "No relevant memories found."})
                result: Dict[str, Any] = {}
                if personal:
                    result["your_memories"] = [
                        m.get("memory", str(m)) for m in personal
                    ]
                if team:
                    result["team_memories"] = [
                        m.get("memory", str(m)) for m in team
                    ]
                if other:
                    result["other_memories"] = [
                        {
                            "actors": m.get("user_id", "?"),
                            "memory": m.get("memory", str(m)),
                        }
                        for m in other
                    ]
                return json.dumps(result)
            except RuntimeError:
                return json.dumps({"error": "Memory service temporarily unavailable."})
            except Exception as exc:
                logger.warning("mem0_oss tool error (mem0_search): %s", exc)
                return json.dumps({"error": str(exc)})

        if tool_name == "mem0_profile":
            try:
                memories = self._get_all()
                if not memories:
                    return json.dumps({"result": "No memories stored yet."})
                return json.dumps({
                    "memories": [m.get("memory", str(m)) for m in memories]
                })
            except RuntimeError:
                return json.dumps({"error": "Memory service temporarily unavailable."})
            except Exception as exc:
                logger.warning("mem0_oss tool error (mem0_profile): %s", exc)
                return json.dumps({"error": str(exc)})

        # if tool_name == "mem0_conclude":
        #     try:
        #         scope = args.get("scope", "personal")
        #         stored = self._add(
        #             [{"role": "user", "content": args["fact"]}],
        #             scope=scope,
        #             infer=False,
        #         )
        #         return json.dumps({"result": f"Memory stored ({scope}).", "stored": stored})
        #     except RuntimeError:
        #         return json.dumps({"error": "Memory service temporarily unavailable."})
        #     except Exception as exc:
        #         logger.warning("mem0_oss tool error (mem0_conclude): %s", exc)
        #         return json.dumps({"error": str(exc)})

        # logger.debug("mem0_oss.handle_tool_call: unknown tool %s", tool_name)
        return json.dumps({"error": f"Unknown tool: {tool_name}"})


def register(collector) -> None:
    logger.info("mem0_oss: register() called, registering Mem0OssProvider")
    collector.register_memory_provider(Mem0OssProvider())
