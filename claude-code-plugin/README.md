# mem0 Claude Code Plugin

Adds long-term memory to Claude Code via two hooks:

- **`mem0_prefetch.py`** (`UserPromptSubmit`) — searches mem0 and injects relevant memories before each LLM call
- **`mem0_sync.py`** (`Stop`) — queues the last conversation turn for async sync, returns immediately
- **`mem0_daemon.py`** — background process that drains the queue and calls `sync_turn()` (auto-started by the Stop hook)

## Setup

### 1. Configure mem0 credentials

Create `~/.claude/mem0.env`:

```env
MEM0_URL=https://your-mem0-server.example.com
MEM0_USER_ID=YourName
MEM0_AGENT_ID=claude-code
# MEM0_CUSTOM_INSTRUCTIONS=
```

### 2. Register the hooks

Add to `~/.claude/settings.json` (global — all projects):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/cai-krew/claude-code-plugin/mem0_prefetch.py"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/cai-krew/claude-code-plugin/mem0_sync.py"
          }
        ]
      }
    ]
  }
}
```

Or add to `<project>/.claude/settings.json` for project-scoped activation only.
Both levels can coexist — they merge, not override.

### 3. Install dependency

```bash
pip install httpx
```

`mem0_client` (the library these hooks use) must be present one directory above `claude-code-plugin/`.
When distributing to a new machine, copy both `claude-code-plugin/` and `mem0_client/` to the same parent directory.

## How it works

```
User types message
  → UserPromptSubmit hook fires
  → mem0_prefetch.py searches 3 tiers: personal / team / other actors
  → Results injected as <memory-context> before Claude sees the message

Claude responds
  → Stop hook fires
  → mem0_sync.py writes turn to ~/.claude/mem0_queue/<timestamp>.json
  → Starts mem0_daemon.py if not already running
  → Exits immediately (non-blocking)

Background (mem0_daemon.py)
  → Polls ~/.claude/mem0_queue/ every 5 seconds
  → Calls sync_turn() for each queued task (~30-60s per turn)
  → Deletes task file when done
  → Exits after 5 minutes idle (restarted automatically on next turn)
```

## Files created at runtime

| Path | Purpose |
|------|---------|
| `~/.claude/mem0.env` | Credentials config |
| `~/.claude/mem0_queue/` | Pending sync tasks (one JSON file per turn) |
| `~/.claude/mem0_daemon.pid` | Daemon PID (for alive-check) |
| `~/.claude/mem0_daemon.log` | Daemon log output |

## Troubleshooting

- **No memory context injected**: check `MEM0_URL` in `~/.claude/mem0.env` and that the server is reachable
- **Hook not firing**: verify the path in `settings.json` matches the scripts' actual location
- **Import error**: confirm `mem0_client/` exists one level above `claude-code-plugin/`
- **Sync not happening**: check `~/.claude/mem0_daemon.log` for errors; check if tasks pile up in `~/.claude/mem0_queue/`
