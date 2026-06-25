#!/usr/bin/env python3
"""Claude Code Stop hook: sync last conversation turn to mem0.

Fires after each response. Reads the last user+assistant turn from
the session JSONL and calls sync_turn() to store memories.

Note: sync_turn() blocks until mem0 LLM extraction completes (~30-60s).
This is Option A (synchronous). The session will appear "done" only after
the sync completes. Upgrade to Option B (background cache) to avoid this.

Configuration (create ~/.claude/mem0.env):
    MEM0_URL=https://your-mem0-server.example.com
    MEM0_USER_ID=YourName
    MEM0_AGENT_ID=claude-code
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _find_session_jsonl(session_id: str) -> Path | None:
    projects_dir = Path.home() / ".claude" / "projects"
    for jsonl in projects_dir.glob(f"**/{session_id}.jsonl"):
        return jsonl
    return None


def _extract_text(content) -> str:
    """Extract plain text from a message content field (string or list of blocks)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "thinking":
                pass  # skip thinking blocks
        return " ".join(parts).strip()
    return ""


def _get_last_turn(jsonl_path: Path) -> tuple[str | None, str | None]:
    """Return (user_msg, assistant_msg) for the last complete turn."""
    entries = []
    for line in jsonl_path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") in ("user", "assistant"):
            entries.append(entry)

    # Walk backwards: find last assistant text, then its preceding user text
    asst_text: str | None = None
    user_text: str | None = None

    for entry in reversed(entries):
        etype = entry.get("type")
        content = entry.get("message", {}).get("content", "")

        if asst_text is None and etype == "assistant":
            text = _extract_text(content)
            if text:
                asst_text = text

        elif asst_text is not None and etype == "user":
            # Only accept plain-string content (real user message, not tool result)
            if isinstance(content, str) and content.strip():
                user_text = content.strip()
                break

    return user_text, asst_text


def main() -> None:
    _load_env_file(Path.home() / ".claude" / "mem0.env")

    if not os.getenv("MEM0_URL"):
        sys.exit(0)

    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    session_id = data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    jsonl = _find_session_jsonl(session_id)
    if not jsonl:
        sys.exit(0)

    user_msg, asst_msg = _get_last_turn(jsonl)
    if not user_msg or not asst_msg:
        sys.exit(0)

    try:
        from mem0_client import Mem0Client  # noqa: PLC0415

        client = Mem0Client.from_env(agent_id="claude-code", run_id=session_id)
        client.sync_turn(user_msg, asst_msg)
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
