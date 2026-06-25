#!/usr/bin/env python3
"""Claude Code Stop hook: queue last conversation turn for async mem0 sync.

Writes the turn to ~/.claude/mem0_queue/ and returns immediately.
A background daemon (mem0_daemon.py) handles the actual sync_turn() call.

Configuration (create ~/.claude/mem0.env):
    MEM0_URL=https://your-mem0-server.example.com
    MEM0_USER_ID=YourName
    MEM0_AGENT_ID=claude-code
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

QUEUE_DIR = Path.home() / ".claude" / "mem0_queue"
PID_FILE  = Path.home() / ".claude" / "mem0_daemon.pid"
DAEMON    = Path(__file__).parent / "mem0_daemon.py"


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
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return ""


def _get_last_turn(jsonl_path: Path) -> tuple[str | None, str | None]:
    entries = []
    for line in jsonl_path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") in ("user", "assistant"):
            entries.append(entry)

    asst_text: str | None = None
    user_text: str | None = None

    for entry in reversed(entries):
        etype   = entry.get("type")
        content = entry.get("message", {}).get("content", "")

        if asst_text is None and etype == "assistant":
            text = _extract_text(content)
            if text:
                asst_text = text

        elif asst_text is not None and etype == "user":
            if isinstance(content, str) and content.strip():
                user_text = content.strip()
                break

    return user_text, asst_text


def _daemon_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # signal 0 = check existence only
        return True
    except (ValueError, OSError):
        return False


def _start_daemon() -> None:
    subprocess.Popen(
        [sys.executable, str(DAEMON)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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

    # Write task to queue
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    task_file = QUEUE_DIR / f"{int(time.time() * 1000)}_{session_id[:8]}.json"
    task_file.write_text(json.dumps({
        "user_msg":   user_msg,
        "asst_msg":   asst_msg,
        "session_id": session_id,
    }))

    # Ensure daemon is running
    if not _daemon_running():
        _start_daemon()

    sys.exit(0)


if __name__ == "__main__":
    main()
