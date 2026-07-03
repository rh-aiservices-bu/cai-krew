"""mem0 MCP server for Claude Code.

Exposes mem0 search and profile tools so Claude Code can actively query
long-term memory mid-conversation, in addition to the passive prefetch hook.

Requires:
    pip install fastmcp

Config (same env vars as the hooks, loaded from ~/.claude/mem0.env):
    MEM0_URL          Base URL of mem0 server
    MEM0_USER_ID      Your name / user ID
    MEM0_AGENT_ID     Agent ID (default: claude-code)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Load ~/.claude/mem0.env so the server picks up credentials automatically
_env_file = Path.home() / ".claude" / "mem0.env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Ensure mem0_client is importable when running from the plugin directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastmcp import FastMCP
from mem0_client import Mem0Client

mcp = FastMCP("mem0")
_client: Mem0Client | None = None


def _get_client() -> Mem0Client:
    global _client
    if _client is None:
        # Use a longer timeout than the default — MCP calls are explicit/interactive
        # and the mem0 search endpoint can take 20-30s (involves an LLM call).
        os.environ.setdefault("MEM0_TIMEOUT", "45")
        _client = Mem0Client.from_env()
    return _client


@mcp.tool()
def mem0_search(query: str, top_k: int = 5) -> str:
    """Search long-term memory for facts relevant to a query.

    Returns three tiers: your memories (personal), team memories (shared),
    and other actors' memories (lower confidence).
    """
    return _get_client().handle_tool_call("mem0_search", {"query": query, "top_k": top_k})


@mcp.tool()
def mem0_profile() -> str:
    """Retrieve all stored memories for the current user/agent pair."""
    return _get_client().handle_tool_call("mem0_profile", {})


if __name__ == "__main__":
    mcp.run()
