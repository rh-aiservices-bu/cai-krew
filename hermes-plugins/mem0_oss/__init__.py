"""Mem0 self-hosted memory provider for Hermes Agent.

Connects directly to a self-hosted Mem0 server via REST.
No SDK, no cloud account required.

Config (env vars or ~/.hermes/.env):
  MEM0_URL      Base URL of your Mem0 server (required)
  MEM0_USER_ID  User scope for memories — falls back to Hermes's user_id kwarg (default: hermes-user)
  MEM0_AGENT_ID Agent scope override — if not set, uses hermes_{agent_identity} (default: hermes)

Scoping behaviour:
  - Writes include user_id + agent_id + run_id (session_id from Hermes)
  - Reads filter by user_id only → cross-session, cross-agent recall
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0
_CB_THRESHOLD = 5       # failures before circuit breaker trips
_CB_COOLDOWN  = 120.0   # seconds before retry after trip


class Mem0OssProvider(MemoryProvider):

    @property
    def name(self) -> str:
        return "mem0_oss"

    def __init__(self) -> None:
        self._url: str = ""
        self._user_id: str = "hermes-user"
        self._agent_id: str = "hermes"
        self._run_id: str = ""
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
            self._agent_id = f"hermes_{kwargs['agent_identity']}"
        else:
            self._agent_id = "hermes"
        self._run_id   = session_id
        self._client   = httpx.Client(base_url=self._url, timeout=_TIMEOUT)
        logger.info(
            "mem0_oss: connected to %s (user=%s, agent=%s, run=%s)",
            self._url, self._user_id, self._agent_id, self._run_id,
        )

    def shutdown(self) -> None:
        if self._client:
            self._client.close()

    # ── Circuit breaker ──────────────────────────────────────────────────────

    def _cb_ok(self) -> bool:
        if self._cb_failures >= _CB_THRESHOLD:
            if time.time() - self._cb_tripped_at < _CB_COOLDOWN:
                return False
            self._cb_failures = 0  # cooldown elapsed, reset
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

    def _search(self, query: str, top_k: int = 5) -> List[Dict]:
        data = self._request("POST", "/search", json={
            "query":   query,
            "filters": {"user_id": self._user_id},
            "top_k":   top_k,
        })
        return data if isinstance(data, list) else data.get("results", [])

    def _add(self, messages: List[Dict], infer: bool = True) -> None:
        self._request("POST", "/memories", json={
            "messages":  messages,
            "user_id":   self._user_id,
            "agent_id":  self._agent_id,
            "run_id":    self._run_id,
            "infer":     infer,
        })

    def _get_all(self) -> List[Dict]:
        data = self._request("GET", "/memories", params={"user_id": self._user_id})
        return data if isinstance(data, list) else data.get("results", [])

    # ── Per-turn hooks ───────────────────────────────────────────────────────

    def system_prompt_block(self) -> str:
        return (
            "You have persistent long-term memory via mem0. "
            "Use `mem0_search` to recall relevant facts before answering, "
            "`mem0_profile` to review everything stored about the user, "
            "and `mem0_conclude` to save a specific fact verbatim."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return cached search results from the previous turn's background fetch."""
        with self._lock:
            result, self._prefetch_cache = self._prefetch_cache, ""
        return result

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Kick off a background search so results are ready before the next turn."""
        threading.Thread(target=self._bg_prefetch, args=(query,), daemon=True).start()

    def _bg_prefetch(self, query: str) -> None:
        try:
            memories = self._search(query, top_k=5)
            if memories:
                lines = "\n".join(f"- {m.get('memory', m)}" for m in memories)
                with self._lock:
                    self._prefetch_cache = f"## Relevant memories:\n{lines}"
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
                "description": "Search long-term memory for facts relevant to a query.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to search for",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Max number of results to return (default 5)",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "mem0_profile",
                "description": "Retrieve all stored memories about the user.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "name": "mem0_conclude",
                "description": (
                    "Store a specific fact about the user verbatim "
                    "(skips LLM extraction — stored exactly as given)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "fact": {
                            "type": "string",
                            "description": "The fact to store",
                        },
                    },
                    "required": ["fact"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            if tool_name == "mem0_search":
                memories = self._search(args["query"], top_k=args.get("top_k", 5))
                if not memories:
                    return json.dumps({"result": "No relevant memories found."})
                return json.dumps({"memories": [m.get("memory", str(m)) for m in memories]})

            if tool_name == "mem0_profile":
                memories = self._get_all()
                if not memories:
                    return json.dumps({"result": "No memories stored yet."})
                return json.dumps({"memories": [m.get("memory", str(m)) for m in memories]})

            if tool_name == "mem0_conclude":
                self._add(
                    [{"role": "user", "content": args["fact"]}],
                    infer=False,  # store verbatim, no LLM extraction
                )
                return json.dumps({"result": "Memory stored."})

        except RuntimeError:
            return json.dumps({"error": "Memory service temporarily unavailable."})
        except Exception as exc:
            logger.warning("mem0_oss tool error: %s", exc)
            return json.dumps({"error": str(exc)})

        raise NotImplementedError(tool_name)


def register():
    return Mem0OssProvider
