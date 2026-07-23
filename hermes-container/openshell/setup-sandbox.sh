#!/bin/bash
# Create and configure a Hermes agent sandbox on OpenShell.
# Equivalent to: helm upgrade --install hermes-agent hermes-container/chart ...
#
# Prerequisites:
#   - OpenShell gateway deployed and registered (openshell gateway add ...)
#   - oc logged in (for MLflow token)
#
# Usage:
#   bash setup-sandbox.sh [sandbox-name]
#
# Required env vars:
#   OPENAI_BASE_URL       LLM base URL (no trailing /v1)
#   OPENAI_API_KEY        LLM API key
#   OPENAI_MODEL          Model name (e.g. Qwen3.6-35B-A3B)
#
# Optional env vars:
#   SANDBOX_IMAGE         Pre-baked image. If set, uses --from to skip runtime install.
#                         Default: quay.io/rlundber/hermes-openshell:0.1
#   SANDBOX_NAME          Sandbox name (default: hermes)
#   PLATFORM              Messaging platform: slack (default) or mattermost
#
#   # Slack (when PLATFORM=slack)
#   SLACK_BOT_TOKEN       xoxb-...
#   SLACK_APP_TOKEN       xapp-...
#   SLACK_ALLOWED_USERS   Comma-separated Slack user IDs
#   SLACK_HOME_CHANNEL    Channel ID for home notifications
#   SLACK_TRIAGE          true/false — autonomous triage mode (default: false)
#
#   # Mattermost (when PLATFORM=mattermost)
#   MATTERMOST_URL        https://mattermost.example.com
#   MATTERMOST_TOKEN      Bot token
#   MATTERMOST_TEAM       Team name
#
#   # mem0
#   MEM0_URL              http://mem0-server.cai-crew.svc.cluster.local:8000
#   MEM0_AGENT_ID         Agent ID in mem0 (default: sandbox-name)
#
#   # MLflow
#   MLFLOW_TRACKING_URI   https://rh-ai.apps.example.com/mlflow
#   MLFLOW_WORKSPACE      MLflow workspace (default: cai-crew)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SANDBOX_NAME="${1:-hermes}"
PLATFORM="${PLATFORM:-slack}"
SANDBOX_IMAGE="${SANDBOX_IMAGE:-quay.io/rlundber/hermes-openshell:0.1}"

# ── Validate required vars ────────────────────────────────────────────────────
for var in OPENAI_BASE_URL OPENAI_API_KEY OPENAI_MODEL; do
    if [ -z "${!var:-}" ]; then
        echo "ERROR: $var is not set" >&2
        exit 1
    fi
done

# ── Defaults ──────────────────────────────────────────────────────────────────
MEM0_URL="${MEM0_URL:-http://mem0-server.cai-crew.svc.cluster.local:8000}"
MEM0_AGENT_ID="${MEM0_AGENT_ID:-$SANDBOX_NAME}"
SLACK_TRIAGE="${SLACK_TRIAGE:-false}"
MLFLOW_WORKSPACE="${MLFLOW_WORKSPACE:-cai-crew}"
OCP_TOKEN=$(oc whoami -t 2>/dev/null || true)
MEM0_CUSTOM_INSTRUCTIONS="Always refer to the user by their actual name in stored memories. The user's name can be derived from the user_id field. When a new fact relates to the same subject as an existing memory, prefer UPDATE over ADD and merge the information into a single consolidated memory. Only use ADD when the fact is genuinely new with no overlap."

echo "============================================"
echo " Hermes OpenShell Sandbox Setup"
echo "============================================"
echo " Sandbox : $SANDBOX_NAME"
echo " Platform: $PLATFORM"
echo " Model   : $OPENAI_MODEL"
echo " mem0    : $MEM0_URL"
echo "============================================"
echo ""

# ── Register LLM provider ─────────────────────────────────────────────────────
echo "→ Registering LLM provider..."
openshell provider delete hermes-llm 2>/dev/null || true
openshell provider create \
    --name hermes-llm \
    --type openai \
    --credential "OPENAI_API_KEY=${OPENAI_API_KEY}" \
    --config "base_url=${OPENAI_BASE_URL}"

# ── Create sandbox ────────────────────────────────────────────────────────────
echo "→ Creating sandbox: $SANDBOX_NAME..."
openshell sandbox delete "$SANDBOX_NAME" 2>/dev/null || true
sleep 3

CREATE_ARGS=(--name "$SANDBOX_NAME" --no-tty)
if [ -n "${SANDBOX_IMAGE:-}" ]; then
    echo "  Using pre-baked image: $SANDBOX_IMAGE"
    CREATE_ARGS+=(--from "$SANDBOX_IMAGE")
fi
openshell sandbox create "${CREATE_ARGS[@]}" -- echo "sandbox created" 2>&1 || true

echo "→ Waiting for sandbox to be Ready..."
for i in $(seq 1 30); do
    STATUS=$(openshell sandbox list 2>/dev/null | grep "$SANDBOX_NAME" | sed 's/\x1b\[[0-9;]*m//g' | awk '{print $NF}')
    if [ "$STATUS" = "Ready" ]; then
        echo "  Ready."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "ERROR: Sandbox did not become Ready within 150s" >&2
        exit 1
    fi
    sleep 5
done

# ── Install Hermes if no pre-baked image ─────────────────────────────────────
if [ -z "${SANDBOX_IMAGE:-}" ]; then
    echo "→ Installing Hermes agent in sandbox..."
    openshell sandbox exec --name "$SANDBOX_NAME" -- bash -c '
        pip3 install --user --no-cache-dir "hermes-agent[slack,messaging]" 2>&1 | tail -5
        export PATH="/sandbox/.local/bin:$PATH"
        hermes --version
    '
else
    echo "→ Verifying Hermes agent..."
    openshell sandbox exec --name "$SANDBOX_NAME" -- hermes --version
fi

# ── Upload Hermes config ──────────────────────────────────────────────────────
echo "→ Uploading Hermes config..."
sed \
    -e "s|\${OPENAI_BASE_URL}|${OPENAI_BASE_URL}|g" \
    -e "s|\${OPENAI_API_KEY}|${OPENAI_API_KEY}|g" \
    -e "s|\${OPENAI_MODEL}|${OPENAI_MODEL}|g" \
    -e "s|\${SLACK_TRIAGE}|${SLACK_TRIAGE}|g" \
    "$SCRIPT_DIR/config/hermes-config.yaml.template" > /tmp/hermes-config.yaml

openshell sandbox exec --name "$SANDBOX_NAME" -- mkdir -p /sandbox/.hermes
openshell sandbox upload "$SANDBOX_NAME" /tmp/hermes-config.yaml /sandbox/.hermes/config.yaml

# ── Upload soul ───────────────────────────────────────────────────────────────
if [ -f "$SCRIPT_DIR/config/SOUL.md" ]; then
    echo "→ Uploading soul..."
    openshell sandbox upload "$SANDBOX_NAME" "$SCRIPT_DIR/config/SOUL.md" /sandbox/.hermes/SOUL.md
fi

# ── Write and upload init script ──────────────────────────────────────────────
echo "→ Uploading environment init script..."
cat > /tmp/sandbox-init.sh << INITHEADER
#!/bin/sh
# Hermes sandbox environment — auto-sourced on login.

# LLM
export OPENAI_BASE_URL="${OPENAI_BASE_URL}"
export OPENAI_API_KEY="${OPENAI_API_KEY}"
export OPENAI_MODEL="${OPENAI_MODEL}"

# Hermes
export HERMES_HOME="/sandbox/.hermes"
export PATH="/sandbox/.local/bin:\$PATH"

# mem0
export MEM0_URL="${MEM0_URL}"
export MEM0_AGENT_ID="${MEM0_AGENT_ID}"
export MEM0_CUSTOM_INSTRUCTIONS="${MEM0_CUSTOM_INSTRUCTIONS}"

INITHEADER

# Platform-specific tokens
if [ "$PLATFORM" = "slack" ]; then
cat >> /tmp/sandbox-init.sh << SLACKENV
# Slack
export SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-}"
export SLACK_APP_TOKEN="${SLACK_APP_TOKEN:-}"
export SLACK_SOCKET_MODE="true"
export SLACK_ALLOWED_USERS="${SLACK_ALLOWED_USERS:-}"
export SLACK_HOME_CHANNEL="${SLACK_HOME_CHANNEL:-}"
export SLACK_TRIAGE="${SLACK_TRIAGE}"

SLACKENV
elif [ "$PLATFORM" = "mattermost" ]; then
cat >> /tmp/sandbox-init.sh << MMENV
# Mattermost
export MATTERMOST_URL="${MATTERMOST_URL:-}"
export MATTERMOST_TOKEN="${MATTERMOST_TOKEN:-}"
export MATTERMOST_TEAM="${MATTERMOST_TEAM:-}"

MMENV
fi

# MLflow (optional)
if [ -n "${MLFLOW_TRACKING_URI:-}" ]; then
cat >> /tmp/sandbox-init.sh << MLFLOWENV
# MLflow
export MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI}"
export MLFLOW_TRACKING_TOKEN="${OCP_TOKEN}"
export MLFLOW_TRACKING_INSECURE_TLS="true"
export MLFLOW_WORKSPACE="${MLFLOW_WORKSPACE}"
export MLFLOW_EXPERIMENT_NAME="hermes-sandbox"

MLFLOWENV
fi

cat >> /tmp/sandbox-init.sh << 'INITFOOTER'
export SANDBOX_ENV_LOADED=1
echo "Hermes environment loaded. Run: hermes"
INITFOOTER

openshell sandbox upload "$SANDBOX_NAME" /tmp/sandbox-init.sh /sandbox/.sandbox-init.sh

# ── Auto-source on login ──────────────────────────────────────────────────────
echo "→ Configuring auto-source on login..."
openshell sandbox exec --name "$SANDBOX_NAME" -- sh -c '
    grep -q "sandbox-init.sh" /sandbox/.profile 2>/dev/null || \
    cat >> /sandbox/.profile << '"'"'PROFILE'"'"'

if [ -f /sandbox/.sandbox-init.sh ] && [ -z "$SANDBOX_ENV_LOADED" ]; then
    . /sandbox/.sandbox-init.sh
fi
PROFILE
'

# ── MLflow experiment ─────────────────────────────────────────────────────────
if [ -n "${MLFLOW_TRACKING_URI:-}" ] && [ -n "$OCP_TOKEN" ]; then
    echo "→ Creating MLflow experiment..."
    openshell sandbox exec --name "$SANDBOX_NAME" -- sh -c "
        export MLFLOW_TRACKING_URI='${MLFLOW_TRACKING_URI}'
        export MLFLOW_TRACKING_TOKEN='${OCP_TOKEN}'
        export MLFLOW_TRACKING_INSECURE_TLS=true
        export MLFLOW_WORKSPACE='${MLFLOW_WORKSPACE}'
        python3 -c '
import os, mlflow
mlflow.set_tracking_uri(os.environ[\"MLFLOW_TRACKING_URI\"])
name = \"hermes-sandbox\"
exp = mlflow.get_experiment_by_name(name)
print(exp.experiment_id if exp else mlflow.create_experiment(name))
' 2>&1 || echo 'MLflow setup: non-fatal'
    "
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "============================================"
echo " Sandbox '$SANDBOX_NAME' ready!"
echo "============================================"
echo ""
echo " Connect:"
echo "   openshell sandbox connect $SANDBOX_NAME"
echo ""
echo " Inside the sandbox:"
if [ "$PLATFORM" = "slack" ]; then
echo "   hermes gateway run        # Slack socket mode"
elif [ "$PLATFORM" = "mattermost" ]; then
echo "   hermes gateway run        # Mattermost gateway"
fi
echo "   hermes                    # interactive CLI"
echo ""
