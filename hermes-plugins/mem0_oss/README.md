# mem0_oss — Self-Hosted Mem0 Memory Provider for Hermes

Connects Hermes Agent to a self-hosted Mem0 server via REST.
No SDK, no cloud account required.

## Prerequisites

- Mem0 deployed on OpenShift (see `mem0/mem0-openshift.yaml`)
- Hermes Agent running on the same OpenShift cluster

## Installation

Copy the plugin files into Hermes's user plugin directory:

```bash
mkdir -p /opt/app-root/src/.hermes/plugins/mem0_oss
cp __init__.py plugin.yaml /opt/app-root/src/.hermes/plugins/mem0_oss/
```

> `HERMES_HOME` defaults to `/opt/app-root/src/.hermes/` when running in the OpenShift container.

## Configuration

**1. Add env vars to `$HERMES_HOME/.env`:**

```bash
# Use the internal Kubernetes service URL (not the external route)
# — avoids SSL issues and is faster from within the cluster
MEM0_URL=http://mem0-server:8000

# If Hermes and mem0 are in different namespaces:
# MEM0_URL=http://mem0-server.<mem0-namespace>.svc.cluster.local:8000

MEM0_USER_ID=hermes-user
MEM0_AGENT_ID=hermes
```

**2. Set the memory provider in `$HERMES_HOME/config.yaml`:**

```yaml
memory:
  provider: mem0_oss
```

> Note: this must go in `config.yaml`, **not** `cli-config.yaml`.

## Verify it works

Restart Hermes and check the startup logs for:
```
mem0_oss: connected to http://mem0-server:8000 (user=hermes-user)
```

Then ask Hermes to use its memory tools directly:
```
Search your memory for anything you know about me.
```

You can also confirm memories are being stored by checking the Mem0 dashboard at the route exposed by your OpenShift deployment.

## How it works

| Event | What happens |
|-------|-------------|
| Each turn starts | Cached search results from the previous turn are injected into context |
| After each turn | Conversation is sent to `POST /memories` in a background thread — LLM extracts facts automatically |
| `mem0_search` tool | Agent searches memories semantically via `POST /search` |
| `mem0_profile` tool | Agent retrieves all stored memories via `GET /memories` |
| `mem0_conclude` tool | Agent stores a specific fact verbatim (no LLM extraction) via `POST /memories` with `infer: false` |

A circuit breaker pauses API calls after 5 consecutive failures (2-minute cooldown), so Hermes keeps working even if mem0 is temporarily unavailable.
