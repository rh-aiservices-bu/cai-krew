#!/usr/bin/env python3
"""mem0 background sync daemon.

Reads pending sync tasks from ~/.claude/mem0_queue/, calls sync_turn() for
each, then deletes the task file. Exits after IDLE_TIMEOUT seconds with no
new tasks (the Stop hook restarts it on the next turn).

Not meant to be run directly — started automatically by mem0_sync.py.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

QUEUE_DIR   = Path.home() / ".claude" / "mem0_queue"
PID_FILE    = Path.home() / ".claude" / "mem0_daemon.pid"
LOG_FILE    = Path.home() / ".claude" / "mem0_daemon.log"
POLL_INTERVAL = 5    # seconds between queue scans
IDLE_TIMEOUT  = 300  # seconds idle before daemon exits


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _setup_logging() -> None:
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _write_pid() -> None:
    PID_FILE.write_text(str(os.getpid()))


def _clear_pid() -> None:
    PID_FILE.unlink(missing_ok=True)


def _process_task(task_file: Path) -> None:
    try:
        data = json.loads(task_file.read_text())
        user_msg   = data["user_msg"]
        asst_msg   = data["asst_msg"]
        session_id = data.get("session_id", "")

        from mem0_client import Mem0Client  # noqa: PLC0415

        client = Mem0Client.from_env(agent_id="claude-code", run_id=session_id)
        client.sync_turn(user_msg, asst_msg)
        logging.info("synced task %s", task_file.name)
    except Exception as exc:
        logging.warning("failed to sync %s: %s", task_file.name, exc)
    finally:
        task_file.unlink(missing_ok=True)


def main() -> None:
    _load_env_file(Path.home() / ".claude" / "mem0.env")
    _setup_logging()
    _write_pid()

    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    logging.info("mem0 daemon started (pid=%d)", os.getpid())
    idle_since = time.time()

    try:
        while True:
            tasks = sorted(QUEUE_DIR.glob("*.json"))
            if tasks:
                idle_since = time.time()
                for task_file in tasks:
                    _process_task(task_file)
            else:
                if time.time() - idle_since > IDLE_TIMEOUT:
                    logging.info("idle timeout — exiting")
                    break

            time.sleep(POLL_INTERVAL)
    finally:
        _clear_pid()
        logging.info("mem0 daemon stopped")


if __name__ == "__main__":
    main()
