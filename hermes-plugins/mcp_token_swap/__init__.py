"""MCP per-user token swap plugin for Hermes.

Before each turn, reads the current Slack user's Keycloak token from
  HERMES_HOME/user-tokens/{slack_user_id}/keycloak.json
and atomically copies it to
  HERMES_HOME/mcp-tokens/{server_name}.json

Hermes's disk-watcher (MCPOAuthManager.invalidate_if_disk_changed) detects
the mtime change on the next MCP tool call and transparently reloads the
token -- no restart needed.

If no token exists for the user, or the token is expired, gateway.json is
left untouched and the SA token written by the existing CronJob is used.

Config (env vars):
  MCP_TOKEN_SWAP_SERVER_NAME   MCP server key to swap (default: gateway)
"""
from __future__ import annotations

import json
import logging
import os
import stat
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

_SERVER_NAME = os.environ.get("MCP_TOKEN_SWAP_SERVER_NAME", "gateway")


class McpTokenSwapProvider(MemoryProvider):
    """No-op memory provider that swaps gateway.json to the per-user token before each turn."""

    def __init__(self) -> None:
        self._hermes_home: Optional[Path] = None
        self._user_id: Optional[str] = None

    @property
    def name(self) -> str:
        return "mcp_token_swap"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = kwargs.get("hermes_home")
        self._hermes_home = (
            Path(hermes_home)
            if hermes_home
            else Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        )
        self._user_id = kwargs.get("user_id")
        if self._user_id:
            logger.info("mcp_token_swap: session for user_id=%s", self._user_id)
        else:
            logger.debug("mcp_token_swap: no user_id (CLI/cron), swap disabled")

        # Ensure user-tokens dir exists and is writable by other pods (e.g. auth-ui)
        # that share the same RWO PVC but run as a different UID.
        # Hermes owns the PVC, so this is the right place to guarantee the directory.
        user_tokens_dir = self._hermes_home / "user-tokens"
        try:
            user_tokens_dir.mkdir(mode=0o777, exist_ok=True)
            # Ensure the mode sticks even if the dir already existed with wrong perms.
            user_tokens_dir.chmod(0o777)
        except Exception as exc:
            logger.warning("mcp_token_swap: could not create user-tokens dir: %s", exc)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Swap gateway.json to this user's token before the LLM turn."""
        if self._user_id and self._hermes_home:
            self._swap_token(self._user_id)
        return ""

    def _swap_token(self, user_id: str) -> None:
        user_token_path = (
            self._hermes_home / "user-tokens" / user_id / "keycloak.json"
        )
        gateway_path = self._hermes_home / "mcp-tokens" / f"{_SERVER_NAME}.json"

        if not user_token_path.exists():
            logger.debug(
                "mcp_token_swap: no token for user %s, leaving SA token in place", user_id
            )
            return

        try:
            data = json.loads(user_token_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("mcp_token_swap: failed to read token for %s: %s", user_id, exc)
            return

        expires_at = data.get("expires_at", 0)
        if expires_at and time.time() > float(expires_at):
            logger.warning(
                "mcp_token_swap: token for user %s expired at %s, skipping", user_id, expires_at
            )
            return

        try:
            gateway_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = gateway_path.with_suffix(".swap.tmp")
            fd = os.open(
                str(tmp),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                stat.S_IRUSR | stat.S_IWUSR,
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                try:
                    os.unlink(str(tmp))
                except OSError:
                    pass
                raise
            os.replace(str(tmp), str(gateway_path))
            logger.info("mcp_token_swap: gateway.json swapped to user %s token", user_id)
        except Exception as exc:
            logger.warning("mcp_token_swap: failed to write gateway.json: %s", exc)

    # -- MemoryProvider stubs (this plugin has no memory tools) --------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def system_prompt_block(self) -> str:
        return ""

    def shutdown(self) -> None:
        pass
