#!/usr/bin/env python3
"""Lorekeeper entrypoint.

Reads all memories, asks an LLM to identify redundant, contradictory, or
mergeable memories, then either posts interactive Slack messages for approval
or prints the full report as JSON to stdout.

Required env vars:
  MEM0_URL        Base URL of the mem0 server
  LITELLM_URL     Base URL of the LiteLLM proxy
  OPENAI_API_KEY  API key for the LiteLLM proxy

Optional env vars:
  LITELLM_MODEL      LLM model name (default: Qwen3.6-35B-A3B)
  MEM0_ACTOR_KEYS    Comma-separated actor keys to restrict analysis to specific
                     actors (e.g. "hermes|alice,hermes|bob"). When omitted, actor
                     keys are discovered automatically via GET /memories.
  SLACK_BOT_TOKEN    xoxb-... token. When set, posts interactive approval
                     messages to Slack instead of printing JSON to stdout.
  SLACK_CHANNEL      Slack channel to post to (e.g. #lorekeeper).
                     Required when SLACK_BOT_TOKEN is set.
"""
from __future__ import annotations

import json
import logging
import os
import sys

import pruner
import slack_notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        logger.error("Required env var %s is not set", name)
        sys.exit(1)
    return val


def main() -> None:
    mem0_url      = _require("MEM0_URL")
    litellm_url   = _require("LITELLM_URL")
    api_key       = _require("OPENAI_API_KEY")
    litellm_model = os.environ.get("LITELLM_MODEL", "Qwen3.6-35B-A3B")

    actor_keys_raw = os.environ.get("MEM0_ACTOR_KEYS", "")
    actor_keys = [k.strip() for k in actor_keys_raw.split(",") if k.strip()]

    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL")

    if slack_token and slack_channel:
        # Post each actor's actions to Slack as soon as they're ready
        def on_actions(scope_label, actions):
            slack_notifier.post_actions(slack_token, slack_channel, scope_label, actions)
    else:
        on_actions = None
        if slack_token and not slack_channel:
            logger.warning("SLACK_BOT_TOKEN is set but SLACK_CHANNEL is missing — printing to stdout")

    logger.info(
        "Starting lorekeeper | model=%s | actors=%s | slack=%s",
        litellm_model,
        actor_keys if actor_keys else "(auto-discover)",
        slack_channel or "disabled",
    )

    report = pruner.run(
        mem0_url=mem0_url,
        actor_keys=actor_keys,
        litellm_url=litellm_url,
        litellm_model=litellm_model,
        api_key=api_key,
        on_actions=on_actions,
    )

    s = report["summary"]
    logger.info(
        "Done | total_memories=%d  suggested_deletes=%d  suggested_merges=%d",
        s["total_memories"],
        s["total_deletes"],
        s["total_merges"],
    )

    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL")

    if on_actions:
        try:
            slack_notifier.post_summary(slack_token, slack_channel, s)
        except Exception as exc:
            logger.error("Failed to post summary to Slack: %s", exc)
    else:
        print(json.dumps(report, indent=2))

    if s["total_memories"] == 0:
        logger.warning("No memories found — check MEM0_URL and that memories exist")
        sys.exit(2)


if __name__ == "__main__":
    main()
