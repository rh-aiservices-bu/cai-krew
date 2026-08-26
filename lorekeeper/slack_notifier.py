"""Post applied pruning results to Slack as informational messages."""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

_SLACK_API = "https://slack.com/api"


def _slack_post(token: str, method: str, payload: Dict) -> Dict:
    r = httpx.post(
        f"{_SLACK_API}/{method}",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=10.0,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack {method} error: {data.get('error')}")
    return data


def _action_blocks(action: Dict, scope_label: str) -> List[Dict]:
    """Build Block Kit blocks for a single applied pruning action (info-only)."""
    verb = action.get("action", "").upper()
    reason = action.get("reason", "")
    texts = action.get("texts", {})
    ids = action.get("ids", [])
    result = action.get("result", "")
    result_text = action.get("result_text", "")

    if verb == "DELETE":
        memory_lines = "\n".join(f"> {texts.get(mid, mid)}" for mid in ids)
        body = f"*DELETE* — `{scope_label}`\n{memory_lines}\n_Reason: {reason}_"
    elif verb == "MERGE":
        memory_lines = "\n".join(f"> _{texts.get(mid, mid)}_" for mid in ids)
        new_text = action.get("new_text", "")
        body = (
            f"*MERGE* — `{scope_label}`\n"
            f"{memory_lines}\n"
            f"*Into:* {new_text}\n"
            f"_Reason: {reason}_"
        )
    else:
        return []

    status_emoji = {"applied": "✅", "failed": "❌"}.get(result, "⚠️")
    text = f"{body}\n{status_emoji} _{result_text}_"

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "divider"},
    ]


def post_plan(token: str, channel: str, report: Dict[str, Any]) -> None:
    """Post the full applied pruning results to Slack as informational messages."""
    s = report["summary"]
    total = s["total_deletes"] + s["total_merges"]

    if total == 0:
        _slack_post(token, "chat.postMessage", {
            "channel": channel,
            "text": "Lorekeeper: no changes made today.",
        })
        logger.info("Posted 'no changes' message to Slack")
        return

    # Summary header
    _slack_post(token, "chat.postMessage", {
        "channel": channel,
        "text": f"Lorekeeper: {total} change(s) applied",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"Lorekeeper — {total} change(s) applied",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Analyzed *{s['total_memories']}* memories across "
                        f"*{len(report.get('personal', {}))}* actor(s) + team.\n"
                        f"Applied: *{s['total_deletes']}* delete(s), "
                        f"*{s['total_merges']}* merge(s)."
                    ),
                },
            },
        ],
    })

    posted = 0

    # Personal memory actions
    for actor_key, actor_data in report.get("personal", {}).items():
        if not isinstance(actor_data, dict):
            continue
        for action in actor_data.get("plan", []):
            blocks = _action_blocks(action, actor_key)
            if not blocks:
                continue
            _slack_post(token, "chat.postMessage", {
                "channel": channel,
                "text": f"{action.get('action')} — {actor_key}",
                "blocks": blocks,
            })
            posted += 1

    # Team memory actions
    for action in report.get("team", {}).get("plan", []):
        blocks = _action_blocks(action, "team")
        if not blocks:
            continue
        _slack_post(token, "chat.postMessage", {
            "channel": channel,
            "text": f"{action.get('action')} — team",
            "blocks": blocks,
        })
        posted += 1

    logger.info("Posted %d action message(s) to Slack channel %s", posted, channel)


def post_actions(token: str, channel: str, scope_label: str, actions: List[Dict]) -> None:
    """Post a single actor's applied actions to Slack immediately as they're ready.

    Called by pruner.run() via the on_actions callback after each actor
    completes, so messages appear in Slack in real time rather than all at once.
    """
    posted = 0
    for action in actions:
        blocks = _action_blocks(action, scope_label)
        if not blocks:
            continue
        _slack_post(token, "chat.postMessage", {
            "channel": channel,
            "text": f"{action.get('action')} — {scope_label}",
            "blocks": blocks,
        })
        posted += 1
    if posted:
        logger.info("Posted %d action(s) for %s to Slack", posted, scope_label)


def post_summary(token: str, channel: str, summary: Dict[str, Any]) -> None:
    """Post a final summary message once the full run is complete."""
    total = summary["total_deletes"] + summary["total_merges"]
    if total == 0:
        text = f"Lorekeeper complete — no changes made across {summary['total_memories']} memories."
    else:
        text = (
            f"Lorekeeper complete — analyzed *{summary['total_memories']}* memories, "
            f"applied *{total}* change(s) "
            f"({summary['total_deletes']} delete(s), {summary['total_merges']} merge(s))."
        )
    _slack_post(token, "chat.postMessage", {
        "channel": channel,
        "text": text,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    })
    logger.info("Posted summary to Slack")
