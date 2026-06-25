"""Core pruning logic: fetch → chunk → analyze → collect plan.

v1: report-only. No changes are written to mem0.
v2: apply plan (delete/merge) after Slack approval.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List

from openai import OpenAI

from mem0_client import Mem0Client
from prompts import personal_pruning_messages, team_pruning_messages

logger = logging.getLogger(__name__)

_BATCH_SIZE = 30
_OVERLAP = 5


# ── Chunking ──────────────────────────────────────────────────────────────────

def _chunk_with_overlap(
    items: List,
    batch_size: int = _BATCH_SIZE,
    overlap: int = _OVERLAP,
) -> List[List]:
    """Split a list into batches with a trailing overlap between adjacent batches.

    Example (batch_size=5, overlap=2, 12 items):
      batch 1: items 0-4
      batch 2: items 3-7   (items 3-4 repeated from batch 1)
      batch 3: items 6-10
      batch 4: items 9-11
    """
    if len(items) <= batch_size:
        return [items]
    chunks = []
    start = 0
    while start < len(items):
        end = min(start + batch_size, len(items))
        chunks.append(items[start:end])
        if end == len(items):
            break
        start = end - overlap
    return chunks


# ── LLM call ──────────────────────────────────────────────────────────────────

def _call_llm(llm: OpenAI, model: str, messages: List[Dict]) -> List[Dict]:
    """Call the LLM and parse the JSON plan. Returns a list of action dicts."""
    try:
        response = llm.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        raw = response.choices[0].message.content or "{}"
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("LLM returned non-JSON: %s | raw: %.200s", exc, raw)
        return []
    except Exception as exc:
        logger.warning("LLM call failed: %s", exc)
        return []

    # Accept {"actions": [...]} or bare [...] or other common wrapper keys
    if isinstance(parsed, list):
        return parsed
    for key in ("actions", "results", "changes", "plan"):
        if key in parsed and isinstance(parsed[key], list):
            return parsed[key]

    logger.warning("Unexpected LLM response shape: %s", list(parsed.keys()))
    return []


# ── Plan enrichment ───────────────────────────────────────────────────────────

def _enrich_plan(plan: List[Dict], memories: List[Dict]) -> List[Dict]:
    """Add a 'texts' dict (id → memory text) to each action for human readability."""
    id_to_text = {m["id"]: m.get("memory", "") for m in memories if m.get("id")}
    for action in plan:
        action["texts"] = {
            mid: id_to_text.get(mid, "(not found)")
            for mid in action.get("ids", [])
        }
    return plan


# ── Analysis ──────────────────────────────────────────────────────────────────

def _analyze(
    llm: OpenAI,
    model: str,
    memories: List[Dict],
    messages_fn: Callable[[List[Dict]], List[Dict]],
    label: str,
) -> List[Dict]:
    """Chunk memories with overlap, call LLM on each batch, return combined plan."""
    batches = _chunk_with_overlap(memories)
    plan: List[Dict] = []
    for i, batch in enumerate(batches, start=1):
        logger.info("  %s: batch %d/%d (%d memories)", label, i, len(batches), len(batch))
        actions = _call_llm(llm, model, messages_fn(batch))
        logger.info("  %s: batch %d → %d action(s)", label, i, len(actions))
        plan.extend(actions)
    return plan


# ── Main entry point ──────────────────────────────────────────────────────────

def run(
    mem0_url: str,
    actor_keys: List[str],
    litellm_url: str,
    litellm_model: str,
    api_key: str,
) -> Dict[str, Any]:
    """
    Fetch all memories, analyze them, and return a pruning report.
    Does NOT apply any changes (v1: report only).

    Returns a dict with structure:
      {
        "personal": {
          "<actor_key>": {"memories_count": N, "plan": [...]}
        },
        "team": {"memories_count": N, "plan": [...]},
        "summary": {"total_memories": N, "total_deletes": N, "total_merges": N}
      }
    """
    mem0 = Mem0Client(mem0_url)
    llm = OpenAI(base_url=litellm_url.rstrip("/"), api_key=api_key)

    # Auto-discover actor keys when none are provided
    if not actor_keys:
        logger.info("No actor keys provided — discovering from all memories")
        try:
            actor_keys = mem0.discover_actor_keys()
            logger.info("Discovered %d actor key(s): %s", len(actor_keys), actor_keys)
        except Exception as exc:
            logger.error("Auto-discovery failed: %s", exc)

    report: Dict[str, Any] = {
        "personal": {},
        "team": {},
        "summary": {"total_memories": 0, "total_deletes": 0, "total_merges": 0},
    }

    # ── Personal memories ─────────────────────────────────────────────────────
    for actor_key in actor_keys:
        logger.info("Analyzing personal memories for actor: %s", actor_key)
        try:
            memories = mem0.get_personal_memories(actor_key)
        except Exception as exc:
            logger.error("Failed to fetch memories for %s: %s", actor_key, exc)
            report["personal"][actor_key] = {"error": str(exc)}
            continue

        if not memories:
            logger.info("  No memories found for %s", actor_key)
            report["personal"][actor_key] = {"memories_count": 0, "plan": []}
            continue

        logger.info("  Found %d memories for %s", len(memories), actor_key)
        # Bind actor_key at call time — safe since _analyze runs synchronously
        plan = _enrich_plan(
            _analyze(
                llm, litellm_model, memories,
                lambda batch, ak=actor_key: personal_pruning_messages(ak, batch),
                f"personal[{actor_key}]",
            ),
            memories,
        )
        report["personal"][actor_key] = {"memories_count": len(memories), "plan": plan}
        report["summary"]["total_memories"] += len(memories)

    # ── Team memories ─────────────────────────────────────────────────────────
    logger.info("Analyzing team memories")
    try:
        team_memories = mem0.get_team_memories()
    except Exception as exc:
        logger.error("Failed to fetch team memories: %s", exc)
        report["team"] = {"error": str(exc)}
        team_memories = []

    if team_memories:
        logger.info("  Found %d team memories", len(team_memories))
        team_plan = _enrich_plan(
            _analyze(
                llm, litellm_model, team_memories,
                team_pruning_messages,
                "team",
            ),
            team_memories,
        )
        report["team"] = {"memories_count": len(team_memories), "plan": team_plan}
        report["summary"]["total_memories"] += len(team_memories)
    else:
        report["team"] = {"memories_count": 0, "plan": []}

    # ── Tally summary ─────────────────────────────────────────────────────────
    all_actor_plans = [
        action
        for actor_data in report["personal"].values()
        if isinstance(actor_data, dict)
        for action in actor_data.get("plan", [])
    ]
    all_actions = all_actor_plans + report["team"].get("plan", [])

    for action in all_actions:
        verb = action.get("action", "").upper()
        if verb == "DELETE":
            report["summary"]["total_deletes"] += 1
        elif verb == "MERGE":
            report["summary"]["total_merges"] += 1

    mem0.close()
    return report
