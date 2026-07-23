#!/bin/bash
# Deploy the OpenShell gateway on OpenShift.
# Run this once per cluster before running setup-sandbox.sh.
#
# Usage:
#   bash install-gateway.sh
#
# Environment:
#   NAMESPACE          Target namespace (default: openshell)
#   OPENSHELL_VERSION  Pin a specific chart version (default: latest)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/functions.sh"

NAMESPACE="${NAMESPACE:-openshell}"
OPENSHELL_VERSION="${OPENSHELL_VERSION:-}"

VERSION_FLAG=""
if [ -n "$OPENSHELL_VERSION" ]; then
    VERSION_FLAG="--version $OPENSHELL_VERSION"
fi

echo "============================================"
echo " OpenShell Gateway Install"
echo "============================================"
echo " Namespace: $NAMESPACE"
echo "============================================"
echo ""

check_prereqs

# Step 1: Agent Sandbox CRD
install_agent_sandbox_crd

# Step 2: Namespace
create_openshell_namespace "$NAMESPACE"

# Step 3: SCC
grant_privileged_scc "$NAMESPACE"

# Step 4: JWT signing secret
step "Step 4/7: Create JWT signing secret"
create_jwt_secret "$NAMESPACE"

# Step 5: Helm install
step "Step 5/7: Install OpenShell Helm chart"
# shellcheck disable=SC2086
helm upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart \
    --namespace "$NAMESPACE" \
    $VERSION_FLAG \
    --set pkiInitJob.enabled=false \
    --set server.disableTls=true \
    --set server.auth.allowUnauthenticatedUsers=true \
    --set podSecurityContext.fsGroup=null \
    --set securityContext.runAsUser=null

# Ensure ClusterRoleBinding includes this namespace (a prior install in another
# namespace may own the binding).
EXISTING_NS=$(oc get clusterrolebinding openshell-node-reader -o jsonpath='{.subjects[0].namespace}' 2>/dev/null || true)
if [ -n "$EXISTING_NS" ] && [ "$EXISTING_NS" != "$NAMESPACE" ]; then
    info "Patching ClusterRoleBinding to include $NAMESPACE (currently bound to $EXISTING_NS)"
    oc patch clusterrolebinding openshell-node-reader --type='json' -p="[
      {\"op\": \"add\", \"path\": \"/subjects/-\", \"value\": {\"kind\": \"ServiceAccount\", \"name\": \"openshell\", \"namespace\": \"$NAMESPACE\"}}
    ]"
fi

# Step 6: Wait
step "Step 6/7: Wait for gateway rollout"
wait_for_rollout statefulset openshell "$NAMESPACE" 300

# Step 7: Route
step "Step 7/7: Expose gateway via Route"
oc -n "$NAMESPACE" apply -f "$SCRIPT_DIR/manifests/route.yaml"
sleep 2
GW_ROUTE=$(oc -n "$NAMESPACE" get route openshell-gw -o jsonpath='{.spec.host}' 2>/dev/null || echo "pending")

echo ""
echo "============================================"
echo " Gateway deployed!"
echo "============================================"
echo ""
echo " Gateway URL: http://$GW_ROUTE"
echo ""
echo " Next steps:"
echo ""
echo "   1. Register gateway with CLI:"
echo "      openshell gateway add http://$GW_ROUTE --local --name openshift"
echo ""
echo "   2. Create Hermes sandbox:"
echo "      bash $SCRIPT_DIR/setup-sandbox.sh"
echo ""
