#!/usr/bin/env python3
"""
Pensieve — Importance-driven memory capture and push to Gitea.

Usage:
    python3 pensieve-push.py [options]

Options:
    --config PATH       Path to config JSON (default: config/pensieve-config.json)
    --session ID        Session ID
    --trigger TYPE      Why this was saved (decision, gotcha, preference, lesson, context)
    --extract JSON...   Memory entries to push (JSON strings or plain text)
    --dry-run           Show what would happen without pushing
    --user OVERRIDE     Override user_id from config

Memory structure:
    users/<user_id>/<keywords>-<YYYY-MM-DD>-<HHmm>.md
"""

import json
import os
import sys
import re
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone
from argparse import ArgumentParser


def load_config(config_path=None):
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), "..", "config", "pensieve-config.json")
    with open(config_path, "r") as f:
        config = json.load(f)
    config["user_id"] = config.get("token_name", "unknown")
    return config


def push_file(config, filepath, content, message="Add memory", branch="main", sha=None):
    """Push a file to the Gitea repo via the contents API."""
    base_url = config["gitea_url"]
    owner = config["owner"]
    repo = config["repo"]
    token = config["token"]

    url = f"{base_url}/api/v1/repos/{owner}/{repo}/contents/{filepath}"

    content_b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    payload = {
        "message": message,
        "content": content_b64,
        "branch": branch
    }
    if sha:
        payload["sha"] = sha

    payload_json = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload_json,
        headers={
            "Authorization": f"token {token}",
            "Content-Type": "application/json"
        },
        method="POST" if not sha else "PUT"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return {
                "success": True,
                "sha": result["content"]["sha"],
                "size": result["content"]["size"],
                "filepath": filepath,
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {
            "success": False,
            "error": str(e.code),
            "body": body,
            "filepath": filepath,
        }


def sanitize_filename(name):
    """Convert a string to a safe filesystem name."""
    name = re.sub(r"[^\w\-_\.]", "-", name.lower())
    name = re.sub(r"-{2,}", "-", name)
    name = name.strip("-")
    return name[:100]


def generate_memory_filename(keywords):
    dt = datetime.now(timezone.utc)
    return f"{sanitize_filename(keywords)}-{dt.strftime('%Y-%m-%d')}-{dt.strftime('%H%M')}.md"


def main():
    parser = ArgumentParser(description="Pensieve semantic memory push")
    parser.add_argument("--config", default=None, help="Path to config JSON")
    parser.add_argument("--session", default=None, help="Session ID")
    parser.add_argument("--trigger", default="memory",
                        help="Why this was saved (decision, gotcha, preference, lesson, context)")
    parser.add_argument("--extract", nargs="*", help="Memory entries to push (JSON or plain text)")
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode")
    args = parser.parse_args()

    if not args.session:
        args.session = os.environ.get("PENSIEVE_SESSION", "default")

    config = load_config(args.config)
    user_id = config.get("user_id", "unknown")

    if not args.extract:
        print("[pensieve] No --extract entries provided. Use --extract '{\"title\": ..., \"content\": ...}'")
        return 1

    memories = []
    for entry in args.extract:
        try:
            memories.append(json.loads(entry))
        except json.JSONDecodeError:
            memories.append({"title": "memory", "content": entry})

    if args.dry_run:
        print(f"[pensieve] DRY RUN — Would push {len(memories)} memories")
        for m in memories:
            fn = generate_memory_filename(m.get("title", "memory"))
            print(f"  -> users/{user_id}/{fn}  [trigger={args.trigger}]")
        return 0

    results = []
    for m in memories:
        title = m.get("title", "memory")
        content = m.get("content", str(m))
        fn = generate_memory_filename(title)
        filepath = f"users/{user_id}/{fn}"
        now = datetime.now(timezone.utc)

        body = f"""---
type: memory
extracted: {now.isoformat()}
source: conversation
trigger: {args.trigger}
session_id: {args.session}
---

## {title.replace('_', ' ').title()}
{content.strip()}
"""

        result = push_file(config, filepath, body, message=f"Memory: {title}")
        results.append(result)

        if result["success"]:
            print(f"[pensieve] Pushed: {filepath} (sha={result['sha']})")
        else:
            print(f"[pensieve] FAILED: {filepath} ({result['error']})")
            print(f"  Error: {result.get('body', 'unknown')}")

    return 0 if all(r["success"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
