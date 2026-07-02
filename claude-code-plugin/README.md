# mem0 Claude Code Plugin

Adds long-term memory to Claude Code via two hooks:

- **`mem0_prefetch.py`** (`UserPromptSubmit`) — searches mem0 and injects relevant memories before each LLM call
- **`mem0_sync.py`** (`Stop`) — queues the last conversation turn for async sync, returns immediately
- **`mem0_daemon.py`** — background process that drains the queue and calls `sync_turn()` (auto-started by the Stop hook)

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/rh-aiservices-bu/cai-krew.git
cd cai-krew
```

The repo contains two sibling directories that must stay together:

```
cai-krew/
├── claude-code-plugin/   ← hooks and daemon
└── mem0_client/          ← shared client library (imported by the hooks)
```

### 2. Note the absolute path to `claude-code-plugin`

```bash
cd claude-code-plugin
pwd
# e.g. /Users/you/cai-krew/claude-code-plugin
```

You'll use this path in the next steps — replace `/path/to/claude-code-plugin` with what `pwd` printed.

### 3. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install httpx
```

### 4. Configure mem0 credentials

Create `~/.claude/mem0.env`:

```env
MEM0_URL=<ask-your-admin>
MEM0_USER_ID=<your-name>
MEM0_AGENT_ID=claude-code
MEM0_CUSTOM_INSTRUCTIONS="Always refer to the user by their actual name in stored memories. The user's name can be derived from the user_id field — for  example, if user_id is 'alice|research-agent-1', the user's name is 'Alice'. Never use generic terms like 'User' or 'The user'. When a new fact relates to the  same subject as an existing memory, prefer UPDATE over ADD and merge the information into a single consolidated memory. Only use ADD when the fact is genuinely new with no overlap."
```

### 5. Register the hooks

Add to `~/.claude/settings.json` (global — all projects), replacing `/path/to/claude-code-plugin` with the path from step 2:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/claude-code-plugin/.venv/bin/python /path/to/claude-code-plugin/mem0_prefetch.py"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/claude-code-plugin/.venv/bin/python /path/to/claude-code-plugin/mem0_sync.py"
          }
        ]
      }
    ]
  }
}
```

> Use the full path to the `.venv` Python interpreter (not bare `python3`) so the hooks run with the correct virtualenv regardless of shell environment.

Or add to `<project>/.claude/settings.json` for project-scoped activation only.
Both levels can coexist — they merge, not override.

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
