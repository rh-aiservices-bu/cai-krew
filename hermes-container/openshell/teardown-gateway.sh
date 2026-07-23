#!/bin/bash
# Remove the OpenShell gateway and all associated resources.
#
# Usage:
#   bash teardown-gateway.sh [--crd]
#
# Flags:
#   --crd   Also delete the Agent Sandbox CRDs (affects all namespaces)
#
# Environment:
#   NAMESPACE   Namespace to tear down (default: openshell)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/functions.sh"

NAMESPACE="${NAMESPACE:-openshell}"
DELETE_CRDS="${1:-}"

echo "============================================"
echo " OpenShell Gateway Teardown"
echo "============================================"
echo " Namespace: $NAMESPACE"
echo "============================================"
echo ""

step "Delete OpenShell Helm release"
helm uninstall openshell --namespace "$NAMESPACE" 2>/dev/null || warn "Helm release not found"

step "Delete Route and secrets"
oc -n "$NAMESPACE" delete route openshell-gw 2>/dev/null || true
oc -n "$NAMESPACE" delete secret openshell-jwt-keys 2>/dev/null || true
oc -n "$NAMESPACE" delete pvc openshell-data-openshell-0 2>/dev/null || true

step "Remove privileged SCC binding"
oc adm policy remove-scc-from-user privileged -z openshell-sandbox -n "$NAMESPACE" 2>/dev/null || true

step "Delete namespace"
oc delete ns "$NAMESPACE" 2>/dev/null || true

if [ "$DELETE_CRDS" = "--crd" ]; then
    step "Delete Agent Sandbox CRDs (cluster-wide)"
    oc delete -f \
        https://github.com/kubernetes-sigs/agent-sandbox/releases/latest/download/sandbox.yaml \
        2>/dev/null || true
fi

echo ""
info "Teardown complete."
