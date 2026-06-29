"""Post the pruning plan to Slack as interactive approval messages.

Each action (DELETE or MERGE) becomes a separate Slack message with
Approve / Skip buttons. The button value encodes the full action so the
approver service can act on it without needing shared state.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

_SLACK_API = "https://slack.com/api"
# Slack's hard limit on button value length
_MAX_VALUE_LEN = 2000


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
    """Build Block Kit blocks for a single pruning action."""
    verb = action.get("action", "").upper()
    reason = action.get("reason", "")
    texts = action.get("texts", {})
    ids = action.get("ids", [])

    if verb == "DELETE":
        memory_lines = "\n".join(f"> {texts.get(mid, mid)}" for mid in ids)
        header = f"*DELETE* — `{scope_label}`\n{memory_lines}\n_Reason: {reason}_"

    elif verb == "MERGE":
        memory_lines = "\n".join(f"> _{texts.get(mid, mid)}_" for mid in ids)
        new_text = action.get("new_text", "")
        header = (
            f"*MERGE* — `{scope_label}`\n"
            f"{memory_lines}\n"
            f"*Into:* {new_text}\n"
            f"_Reason: {reason}_"
        )
    else:
        return []

    # Encode full action into button value so approver needs no shared state
    action_value = json.dumps({
        "action": verb,
        "ids": ids,
        "new_text": action.get("new_text", ""),
        "scope_label": scope_label,
    })
    if len(action_value) > _MAX_VALUE_LEN:
        logger.warning("Button value too long (%d chars), truncating new_text", len(action_value))
        action_value = json.dumps({
            "action": verb,
            "ids": ids,
            "new_text": action.get("new_text", "")[:200],
            "scope_label": scope_label,
        })

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "👍 Apply"},
                    "style": "primary",
                    "action_id": "approve",
                    "value": action_value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "👎 Skip"},
                    "action_id": "skip",
                    "value": action_value,
                },
            ],
        },
        {"type": "divider"},
    ]


def post_plan(token: str, channel: str, report: Dict[str, Any]) -> None:
    """Post the full pruning plan to Slack as interactive per-action messages."""
    s = report["summary"]
    total = s["total_deletes"] + s["total_merges"]

    if total == 0:
        _slack_post(token, "chat.postMessage", {
            "channel": channel,
            "text": "Lorekeeper: no changes suggested today.",
        })
        logger.info("Posted 'no changes' message to Slack")
        return

    # Summary header
    _slack_post(token, "chat.postMessage", {
        "channel": channel,
        "text": f"Lorekeeper: {total} suggested change(s)",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"Lorekeeper — {total} suggested change(s)",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Analyzed *{s['total_memories']}* memories across "
                        f"*{len(report.get('personal', {}))}* actor(s) + team.\n"
                        f"Suggested: *{s['total_deletes']}* delete(s), "
                        f"*{s['total_merges']}* merge(s).\n"
                        "Use 👍 / 👎 on each item below to approve or skip."
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
    """Post a single actor's actions to Slack immediately as they're found.

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
        text = f"Lorekeeper complete — no changes suggested across {summary['total_memories']} memories."
    else:
        text = (
            f"Lorekeeper complete — analyzed *{summary['total_memories']}* memories, "
            f"posted *{total}* suggested change(s) above "
            f"({summary['total_deletes']} delete(s), {summary['total_merges']} merge(s))."
        )
    _slack_post(token, "chat.postMessage", {
        "channel": channel,
        "text": text,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    })
    logger.info("Posted summary to Slack")
