#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: inject mem0 memories before LLM call.

Stdout is injected into the conversation before the API call.
Never blocks the prompt — any failure exits silently.

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

# mem0_client lives one directory up from this plugin folder
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


def _last_user_text(jsonl_path: Path) -> str | None:
    """Return the text of the most recent human-typed user message."""
    last = None
    for line in jsonl_path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "user":
            continue
        content = entry.get("message", {}).get("content", "")
        # Plain string → real user message (not a tool result)
        if isinstance(content, str) and content.strip():
            last = content.strip()
    return last


def main() -> None:
    _load_env_file(Path.home() / ".claude" / "mem0.env")

    if not os.getenv("MEM0_URL"):
        sys.exit(0)  # not configured — skip silently

    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    session_id = data.get("session_id", "")

    # Claude Code may provide the prompt directly in hook input
    query = (data.get("prompt") or "").strip()

    # Fall back to reading last entry from JSONL
    if not query and session_id:
        jsonl = _find_session_jsonl(session_id)
        if jsonl:
            query = _last_user_text(jsonl) or ""

    if len(query) < 5:
        sys.exit(0)

    try:
        from mem0_client import Mem0Client  # noqa: PLC0415

        client = Mem0Client.from_env(agent_id="claude-code", run_id=session_id)
        results = client.search_all(query)
        if results:
            print(f"<memory-context>\n{results}\n</memory-context>")
    except Exception:
        pass  # never block the prompt

    sys.exit(0)


if __name__ == "__main__":
    main()
