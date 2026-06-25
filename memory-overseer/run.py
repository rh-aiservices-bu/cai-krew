#!/usr/bin/env python3
"""Memory overseer entrypoint.

Reads all memories, asks an LLM to identify redundant, contradictory, or
mergeable memories, and prints the full report as JSON to stdout.
No changes are written to mem0 (v1: report-only).

Required env vars:
  MEM0_URL        Base URL of the mem0 server
  LITELLM_URL     Base URL of the LiteLLM proxy
  OPENAI_API_KEY  API key for the LiteLLM proxy

Optional env vars:
  LITELLM_MODEL   LLM model name (default: Qwen3.6-35B-A3B)
  MEM0_ACTOR_KEYS Comma-separated actor keys to restrict analysis to specific
                  actors (e.g. "hermes|alice,hermes|bob"). When omitted, actor
                  keys are discovered automatically via GET /memories.
"""
from __future__ import annotations

import json
import logging
import os
import sys

import pruner

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

    logger.info(
        "Starting memory overseer | model=%s | actors=%s",
        litellm_model,
        actor_keys if actor_keys else "(auto-discover)",
    )

    report = pruner.run(
        mem0_url=mem0_url,
        actor_keys=actor_keys,
        litellm_url=litellm_url,
        litellm_model=litellm_model,
        api_key=api_key,
    )

    # Full JSON report → stdout (can be piped / captured by the CronJob)
    print(json.dumps(report, indent=2))

    # Human-readable summary → stderr
    s = report["summary"]
    logger.info(
        "Done | total_memories=%d  suggested_deletes=%d  suggested_merges=%d",
        s["total_memories"],
        s["total_deletes"],
        s["total_merges"],
    )

    # Exit non-zero if no memories were found at all (likely a config problem)
    if s["total_memories"] == 0:
        logger.warning("No memories found — check MEM0_URL and that memories exist")
        sys.exit(2)


if __name__ == "__main__":
    main()
