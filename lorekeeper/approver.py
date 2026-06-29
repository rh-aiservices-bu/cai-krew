"""Slack interactivity callback handler (REST mode).

Receives button click payloads from Slack, verifies the signing secret,
then applies or skips the requested memory change in mem0.

Run with:
  python -m uvicorn approver:app --host 0.0.0.0 --port 8000

Required env vars:
  MEM0_URL              Base URL of the mem0 server
  SLACK_BOT_TOKEN       xoxb-... token (for updating messages)
  SLACK_SIGNING_SECRET  From the app's Basic Information page
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, List

import httpx
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Lorekeeper Approver", docs_url=None, redoc_url=None)

MEM0_URL = os.environ["MEM0_URL"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]

_mem0 = httpx.Client(base_url=MEM0_URL.rstrip("/"), timeout=30.0)
_slack = httpx.Client(base_url="https://slack.com/api", timeout=10.0)


# ── Slack signature verification ──────────────────────────────────────────────

def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
        base = f"v0:{timestamp}:{body.decode()}"
        expected = "v0=" + hmac.new(
            SLACK_SIGNING_SECRET.encode(),
            base.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False


# ── mem0 operations ───────────────────────────────────────────────────────────

def _delete_memory(memory_id: str) -> None:
    r = _mem0.delete(f"/memories/{memory_id}")
    r.raise_for_status()


def _update_memory(memory_id: str, new_text: str) -> None:
    r = _mem0.put(f"/memories/{memory_id}", json={"text": new_text})
    r.raise_for_status()


def _apply(action: Dict[str, Any]) -> str:
    verb = action.get("action", "").upper()
    ids: List[str] = action.get("ids", [])

    if verb == "DELETE":
        for mid in ids:
            _delete_memory(mid)
        return f"Deleted {len(ids)} memory(s)."

    if verb == "MERGE":
        new_text = action.get("new_text", "")
        if not new_text:
            raise ValueError("MERGE action has no new_text")
        _update_memory(ids[0], new_text)
        for mid in ids[1:]:
            _delete_memory(mid)
        return f"Merged {len(ids)} memories into one."

    raise ValueError(f"Unknown action verb: {verb}")


# ── Slack message update ──────────────────────────────────────────────────────

def _update_message(channel: str, ts: str, original_blocks: List, result_text: str) -> None:
    new_blocks = [b for b in original_blocks if b.get("type") != "actions"]
    new_blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": result_text},
    })
    try:
        _slack.post(
            "/chat.update",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json={"channel": channel, "ts": ts, "blocks": new_blocks},
        )
    except Exception as exc:
        logger.warning("Failed to update Slack message: %s", exc)


# ── Endpoints ─────────────────────────────────────────────────────────────────

def _handle(action_id: str, action_value: Dict, channel: str, ts: str, original_blocks: List) -> None:
    """Run in background so we can return 200 to Slack within its 3-second deadline."""
    if action_id == "approve":
        try:
            result = _apply(action_value)
            logger.info("Applied: %s", action_value)
            _update_message(channel, ts, original_blocks, f"✅ *Applied* — {result}")
        except Exception as exc:
            logger.error("Failed to apply %s: %s", action_value, exc)
            _update_message(channel, ts, original_blocks, f"❌ *Failed* — {exc}")
    else:
        logger.info("Skipped: %s", action_value.get("action"))
        _update_message(channel, ts, original_blocks, "⏭️ *Skipped*")


@app.post("/slack/interactions")
async def slack_interactions(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not _verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # Parse form data manually (avoids Form(...) consuming the stream before body() call)
    from urllib.parse import parse_qs
    form = parse_qs(body.decode())
    payload_str = (form.get("payload") or [""])[0]
    if not payload_str:
        return JSONResponse({"ok": True})
    data = json.loads(payload_str)
    actions = data.get("actions", [])
    if not actions:
        return JSONResponse({"ok": True})

    btn = actions[0]
    action_id = btn.get("action_id")
    action_value = json.loads(btn.get("value", "{}"))

    channel = data["container"]["channel_id"]
    ts = data["container"]["message_ts"]
    original_blocks = data.get("message", {}).get("blocks", [])

    # Return 200 immediately — Slack requires a response within 3 seconds.
    # The actual mem0 call and message update happen in the background.
    background_tasks.add_task(_handle, action_id, action_value, channel, ts, original_blocks)
    return JSONResponse({"ok": True})


@app.get("/health")
def health():
    return {"ok": True}
