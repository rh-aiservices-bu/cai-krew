# mem0 Claude Code Plugin

Adds long-term memory to Claude Code via two hooks:

- **`mem0_prefetch.py`** (`UserPromptSubmit`) — searches mem0 and injects relevant memories before each LLM call
- **`mem0_sync.py`** (`Stop`) — stores the last conversation turn to mem0 after each response

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

### 3. Install dependency

```bash
pip install httpx
```

`mem0_client` (the library these hooks use) must be present one directory above `claude-code-plugin/`. When distributing this plugin to a new machine, copy both `claude-code-plugin/` and `mem0_client/` to the same parent directory.

## How it works

```
User types message
  → UserPromptSubmit hook fires
  → mem0_prefetch.py searches 3 tiers: personal / team / other actors
  → Results injected as <memory-context> before Claude sees the message

Claude responds
  → Stop hook fires
  → mem0_sync.py reads last user+assistant turn from session JSONL
  → Calls sync_turn(): tries team extraction first, falls back to personal
  → mem0 LLM extracts and stores relevant facts (~30-60s, blocks until done)
```

## Known limitation

The `Stop` hook is **synchronous** — each response is followed by a pause while mem0 extracts memories. This is Option A. Option B (background process + cache file) eliminates the delay and can be built on top of this foundation.

## Troubleshooting

- **No memory context injected**: check that `MEM0_URL` is set in `~/.claude/mem0.env` and the server is reachable
- **Hook not firing**: verify the path in `settings.json` matches where the scripts actually live
- **Import error**: confirm `mem0_client/` exists one level above `claude-code-plugin/`
