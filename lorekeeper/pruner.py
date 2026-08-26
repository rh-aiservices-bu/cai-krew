"""Core pruning logic: fetch → chunk → analyze → apply plan."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

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


# ── Apply ─────────────────────────────────────────────────────────────────────

def _validate_ids(ids: List[str]) -> List[str]:
    """Return only UUID-format IDs; log a warning for any that look like hallucinated indices."""
    valid, bad = [], []
    for mid in ids:
        if _UUID_RE.match(mid):
            valid.append(mid)
        else:
            bad.append(mid)
    if bad:
        logger.warning("Skipping non-UUID id(s) %s — LLM likely used batch indices instead of real IDs", bad)
    return valid


def _delete_tolerant(mem0: Mem0Client, mid: str) -> None:
    """Delete a memory, silently ignoring 404 (already gone)."""
    try:
        mem0.delete_memory(mid)
    except Exception as exc:
        if "404" in str(exc):
            logger.debug("Memory %s already deleted, skipping", mid)
        else:
            raise


def _apply_actions(mem0: Mem0Client, plan: List[Dict]) -> None:
    """Apply all actions in a plan in-place, annotating each with 'result' and 'result_text'."""
    for action in plan:
        verb = action.get("action", "").upper()
        raw_ids: List[str] = action.get("ids", [])
        ids = _validate_ids(raw_ids)

        if not ids:
            action["result"] = "skipped"
            action["result_text"] = f"All IDs were invalid (hallucinated indices): {raw_ids}"
            continue

        try:
            if verb == "DELETE":
                for mid in ids:
                    _delete_tolerant(mem0, mid)
                action["result"] = "applied"
                action["result_text"] = f"Deleted {len(ids)} memory(s)."
            elif verb == "MERGE":
                new_text = action.get("new_text", "")
                if not new_text:
                    raise ValueError("MERGE action has no new_text")
                target = None
                for mid in ids:
                    try:
                        mem0.update_memory(mid, new_text)
                        target = mid
                        break
                    except Exception as exc:
                        if "404" in str(exc):
                            logger.debug("MERGE candidate %s already gone, trying next", mid)
                            continue
                        raise
                if target is None:
                    action["result"] = "skipped"
                    action["result_text"] = "All memories in merge group already deleted."
                    continue
                for mid in ids:
                    if mid != target:
                        _delete_tolerant(mem0, mid)
                action["result"] = "applied"
                action["result_text"] = f"Merged {len(ids)} memories into one (target: {target})."
            else:
                action["result"] = "skipped"
                action["result_text"] = f"Unknown action verb: {verb}"
        except Exception as exc:
            logger.error("Failed to apply %s on %s: %s", verb, ids, exc)
            action["result"] = "failed"
            action["result_text"] = str(exc)


# ── Main entry point ──────────────────────────────────────────────────────────

def run(
    mem0_url: str,
    actor_keys: List[str],
    litellm_url: str,
    litellm_model: str,
    api_key: str,
    on_actions: Optional[Callable[[str, List[Dict]], None]] = None,
    apply: bool = True,
) -> Dict[str, Any]:
    """
    Fetch all memories, analyze them, apply the plan, and return a report.

    When apply=True (default), each action is executed immediately after the
    LLM plan is generated for an actor. Each action dict is annotated with
    'result' ("applied"/"failed"/"skipped") and 'result_text'.

    on_actions(scope_label, actions) is called after apply (if enabled), so
    the callback receives actions that are already resolved.

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
        if apply and plan:
            _apply_actions(mem0, plan)
        report["personal"][actor_key] = {"memories_count": len(memories), "plan": plan}
        report["summary"]["total_memories"] += len(memories)
        if on_actions and plan:
            try:
                on_actions(actor_key, plan)
            except Exception as exc:
                logger.warning("on_actions callback failed for %s: %s", actor_key, exc)

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
        if apply and team_plan:
            _apply_actions(mem0, team_plan)
        report["team"] = {"memories_count": len(team_memories), "plan": team_plan}
        report["summary"]["total_memories"] += len(team_memories)
        if on_actions and team_plan:
            try:
                on_actions("team", team_plan)
            except Exception as exc:
                logger.warning("on_actions callback failed for team: %s", exc)
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
