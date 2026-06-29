"""LLM prompts for memory pruning decisions.

Mirrors the personal/team split used in the mem0_oss plugin for consistency:
- personal: focused on individual facts, preferences, and experiences
- team: focused on project decisions, technical standards, and shared knowledge
"""
from __future__ import annotations

from typing import Dict, List


def _format_memories(memories: List[Dict]) -> str:
    lines = []
    for i, m in enumerate(memories):
        created = (
            (m.get("metadata") or {}).get("created_at")
            or m.get("updated_at")
            or m.get("created_at")
            or "unknown"
        )
        text = m.get("memory", str(m))
        mid = m.get("id", f"missing-id-{i}")
        lines.append(f'[{i}] id="{mid}" created="{created}"\n    {text}')
    return "\n".join(lines)


# ── Personal ──────────────────────────────────────────────────────────────────

_PERSONAL_SYSTEM = """\
You are a memory curator for a personal AI assistant memory store.
Your job is to review a batch of personal memories and identify redundancy,
contradiction, and opportunities to consolidate.

Return a JSON object with an "actions" key containing an array of actions.
Only include actions for memories that need changing — omit memories that are fine.

Action types:
  DELETE  — memory is a duplicate, outdated, superseded, or too trivial to keep
  MERGE   — two or more memories should be combined into one cleaner statement

Rules:
- Prefer more specific and more recent memories over vague or older ones.
- If two memories express the same fact differently, DELETE the weaker one.
- If two memories directly contradict each other, DELETE the older one (use created date).
- If 2-3 closely related memories can be expressed as one complete sentence without
  losing information, MERGE them.
- Do NOT merge memories that cover clearly distinct topics just because they share a subject.
- Keep memory IDs exactly as shown in the id="..." field — never invent or shorten them.
- If nothing needs changing in this batch, return {"actions": []}.

Output format (JSON, no markdown fences):
{
  "actions": [
    {"action": "DELETE", "ids": ["<full-uuid>"], "reason": "<one sentence>"},
    {"action": "MERGE", "ids": ["<uuid1>", "<uuid2>"], "new_text": "<merged statement>", "reason": "<one sentence>"}
  ]
}"""


def personal_pruning_messages(actor_key: str, memories: List[Dict]) -> List[Dict[str, str]]:
    formatted = _format_memories(memories)
    user_content = (
        f"Actor: {actor_key}\n"
        f"Memories in this batch: {len(memories)}\n\n"
        f"--- MEMORIES ---\n{formatted}\n--- END ---\n\n"
        "Review these personal memories and return a JSON object with an "
        '"actions" array for any memories that need changing. '
        'Return {"actions": []} if everything looks clean.'
    )
    return [
        {"role": "system", "content": _PERSONAL_SYSTEM},
        {"role": "user", "content": user_content},
    ]


# ── Team ──────────────────────────────────────────────────────────────────────

_TEAM_SYSTEM = """\
You are a memory curator for a shared team knowledge base.
Team memories capture project decisions, technical standards, architecture choices,
shared processes, and domain knowledge relevant to the whole team.

Return a JSON object with an "actions" key containing an array of actions.
Only include actions for memories that need changing — omit memories that are fine.

Action types:
  DELETE  — memory is a duplicate of another, or directly superseded by a newer decision
  MERGE   — two or more memories cover the same topic and can be expressed as one
            authoritative statement without losing information

Rules:
- Technical decisions and architectural standards can be long-lived — do NOT delete
  them just because they are old. Only delete on direct contradiction or clear supersession.
- If two memories state the same decision differently, DELETE the less precise one.
- If a memory is superseded by a newer decision on the same topic in this batch,
  DELETE the older one (use created date to determine which is newer).
- Keep memory IDs exactly as shown in the id="..." field — never invent or shorten them.
- If nothing needs changing in this batch, return {"actions": []}.

Output format (JSON, no markdown fences):
{
  "actions": [
    {"action": "DELETE", "ids": ["<full-uuid>"], "reason": "<one sentence>"},
    {"action": "MERGE", "ids": ["<uuid1>", "<uuid2>"], "new_text": "<merged statement>", "reason": "<one sentence>"}
  ]
}"""


def team_pruning_messages(memories: List[Dict]) -> List[Dict[str, str]]:
    formatted = _format_memories(memories)
    user_content = (
        f"Team memories in this batch: {len(memories)}\n\n"
        f"--- MEMORIES ---\n{formatted}\n--- END ---\n\n"
        "Review these team memories and return a JSON object with an "
        '"actions" array for any memories that need changing. '
        'Return {"actions": []} if everything looks clean.'
    )
    return [
        {"role": "system", "content": _TEAM_SYSTEM},
        {"role": "user", "content": user_content},
    ]
