---
name: pensieve
description: Capture memories when the LLM judges something is worth saving — no counter, triggers on importance.
---

# Pensieve — Importance-Driven Memory Capture

## Purpose

After every AI response, evaluate whether the conversation just produced something worth remembering. If yes, extract and push immediately to Gitea.

## Trigger

> **This runs automatically. After every response you give, before moving on, silently evaluate the conversation. Do not announce it. Do not ask the user. If something is worth saving, push it immediately. At the end of the session, do a final sweep of the full conversation for anything missed.**

### Save if ANY of these are true:

| Signal | Good example | Bad example (skip) |
|--------|-------------|-------------------|
| Non-obvious problem solved | "OCI helm push requires `oci://` prefix, not `https://`" | "We discussed helm charts" |
| Decision affecting future work | "Team agreed to use Helm over Kustomize for this project" | "User mentioned they like Helm" |
| Preference or convention established | "Always use dark mode, 85% cacao chocolate" | "User seems to prefer dark themes" |
| Gotcha or pitfall discovered | "Gitea token needs `write:repository` scope — `read` silently fails" | "Gitea tokens have scopes" |
| New tool, command, or workflow learned | "`kubectl debug` requires `--profile=restricted` on hardened clusters" | "We used kubectl today" |
| Important project context | "This cluster uses OCP 4.16 with restricted SCCs by default" | "The project uses OpenShift" |

### Skip if ANY of these are true:

- The exchange was purely conversational with no reusable conclusion
- The information is obvious or already well-documented
- The memory would only describe a tool or skill that already has its own documentation (e.g. do not save a summary of pensieve itself)

## Flow

```
After each AI response:
  ├─ Evaluate signals above
  ├─ No signal → do nothing, move on
  └─ Signal detected →
       ├─ Extract the specific, concrete fact (not a summary of the conversation)
       ├─ Push to Gitea
       └─ Do not push the same insight twice in one session

At end of session:
  └─ Sweep the full conversation for any signals that were missed above
```

## Memory Format

Write memories as concrete, standalone facts — not summaries of what happened.

```markdown
---
type: memory
extracted: <ISO timestamp>
source: conversation
trigger: <decision|gotcha|preference|lesson|context>
session_id: <session id if available>
---

## <Category: what kind of knowledge this is>

<One or two sentences. Specific enough to be useful without re-reading the conversation.>

### Details
- <concrete detail, command, value, or constraint>
- <add more only if directly relevant>

### Do not
- <anti-pattern, wrong approach, or thing that breaks — only include if applicable>
```

## Push to Gitea

```bash
python3 /opt/app-root/src/.hermes/skills/pensieve/scripts/pensieve-push.py \
  --trigger <decision|gotcha|preference|lesson|context> \
  --extract '{"title": "helm-oci-push", "content": "Use oci:// prefix, not https://..."}' \
  --extract '{"title": "gitea-token-scope", "content": "Token needs write:repository scope"}'
```

`--trigger` is the signal that fired. `--extract` is the memory content as JSON `{"title": ..., "content": ...}`. Use 2-4 hyphenated keywords for `title` — the script appends the date and time to form the filename. Everything else (Gitea URL, token, repo, folder) is read from `config/pensieve-config.json` automatically.

## Comparison to Counter-Based Pensieve

| | pensieve-counter | pensieve (this skill) |
|---|---|---|
| Trigger | Every N turns | When something important happens |
| Memory quality | Mixed (includes noise) | Higher signal |
| Risk of missing things | Low (fires reliably) | LLM must recognize importance |
| Best for | High-volume sessions | Most sessions |

## Platform Support

| Platform | Hook to use |
|----------|-------------|
| Hermes Agent | `post_llm_call` shell hook |
| Codex CLI | `PostToolUse` or `Stop` hook |
| Claude Code | `Stop` hook |
| Any agent | Run as part of skill prompt after each response |
