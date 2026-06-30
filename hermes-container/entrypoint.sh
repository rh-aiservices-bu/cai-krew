#!/bin/bash
# Hermes Agent UBI9 Container Entrypoint
# Handles runtime config merging and env var injection before starting Hermes

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/app/.hermes}"

echo "=== Hermes Agent Container Startup ==="
echo "HERMES_HOME: ${HERMES_HOME}"
echo "HERMES_PROFILE: ${HERMES_PROFILE:-default}"
echo "HOSTNAME: $(hostname)"

# --- Runtime env var injection ---
# Any HERMES_ENV_ prefixed vars get injected into .env at startup
# This lets you override config per-deployment without rebuilding the image
if [ -d "${HERMES_HOME}" ] && [ -f "${HERMES_HOME}/.env" ]; then
    for var in $(env | grep "^HERMES_ENV_" | cut -d= -f1); do
        key="${var#HERMES_ENV_}"
        val="${!var}"
        echo "Injecting runtime env: ${key}"
        # If key already exists in .env, update it; otherwise append
        if grep -q "^${key}=" "${HERMES_HOME}/.env" 2>/dev/null; then
            sed -i "s|^${key}=.*|${key}=${val}|" "${HERMES_HOME}/.env"
        else
            echo "${key}=${val}" >> "${HERMES_HOME}/.env"
        fi
    done
fi

# --- Runtime config injection ---
# Any HERMES_CONFIG_ prefixed vars get merged into config.yaml
if [ -d "${HERMES_HOME}" ] && [ -f "${HERMES_HOME}/config.yaml" ]; then
    # Check if python3 is available for YAML manipulation
    if command -v python3 &>/dev/null || command -v python &>/dev/null; then
        for var in $(env | grep "^HERMES_CONFIG_" | cut -d= -f1); do
            key="${var#HERMES_CONFIG_}"
            val="${!var}"
            echo "Injecting runtime config: ${key}=${val}"
            python3 -c "
import yaml, sys
path = '${HERMES_HOME}/config.yaml'
with open(path, 'r') as f:
    cfg = yaml.safe_load(f) or {}
keys = '${key}'.split('.')
d = cfg
for k in keys[:-1]:
    d = d.setdefault(k, {})
d[keys[-1]] = yaml.safe_load('${val}')
with open(path, 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False)
" 2>/dev/null || echo "WARN: Could not inject config ${key} (python yaml may not be available)"
        done
    fi
fi

# --- mem0 database directory ---
if [ -z "${MEM0_DB_PATH:-}" ]; then
    MEM0_DB_PATH="${HERMES_HOME}/mem0"
fi
mkdir -p "${MEM0_DB_PATH}"

# --- Log directory ---
mkdir -p "${HERMES_HOME}/logs"

echo "=== Environment Summary ==="
echo "LLM Provider: ${HERMES_MODEL_PROVIDER:-custom}"
echo "LLM Base URL: ${HERMES_MODEL_BASE_URL:-configured in config.yaml}"
echo "Slack Socket Mode: ${SLACK_SOCKET_MODE:-${SLACK_SOCKET_MODE:-false}}"
echo "mem0 URL: ${MEM0_URL:-not configured}"
echo "==========================="

# --- Signal handling for clean shutdown ---
cleanup() {
    echo "Shutdown signal received. Cleaning up..."
    if [ -f "${HERMES_HOME}/gateway.pid" ]; then
        kill $(cat "${HERMES_HOME}/gateway.pid") 2>/dev/null || true
        rm -f "${HERMES_HOME}/gateway.pid"
    fi
    # Clean up lock files
    rm -f "${HERMES_HOME}/gateway.lock"
    exit 0
}
trap cleanup SIGTERM SIGINT

echo "=== Starting Hermes Gateway ==="
exec hermes gateway run --profile "${HERMES_PROFILE:-default}"
