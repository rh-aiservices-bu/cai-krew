#!/bin/bash
# Shared shell helpers for OpenShell gateway and sandbox scripts.
# Adapted from agent-harness-in-a-box/common/functions.sh.

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step()  { echo -e "\n${BLUE}=== $* ===${NC}"; }

check_prereqs() {
    local missing=()
    command -v oc       &>/dev/null || missing+=("oc")
    command -v helm     &>/dev/null || missing+=("helm")
    command -v openshell &>/dev/null || missing+=("openshell")
    if [ ${#missing[@]} -gt 0 ]; then
        error "Missing required tools: ${missing[*]}"
        exit 1
    fi
    if ! oc whoami &>/dev/null; then
        error "Not logged in to OpenShift. Run 'oc login' first."
        exit 1
    fi
    info "Prerequisites OK ($(oc whoami) @ $(oc whoami --show-server))"
}

wait_for_rollout() {
    local type="$1" name="$2" ns="$3" timeout="${4:-300}"
    info "Waiting for $type/$name in $ns (timeout: ${timeout}s)..."
    oc -n "$ns" rollout status "$type/$name" --timeout="${timeout}s"
}

wait_for_pod_ready() {
    local ns="$1" selector="$2" timeout="${3:-120}"
    info "Waiting for pod ($selector) in $ns..."
    oc -n "$ns" wait --for=condition=Ready pod -l "$selector" --timeout="${timeout}s" 2>/dev/null \
        || warn "Pod not ready yet (may still be pulling image)"
}

_find_openssl() {
    for p in /opt/homebrew/opt/openssl@3/bin/openssl \
             /opt/homebrew/opt/openssl/bin/openssl \
             /usr/local/opt/openssl@3/bin/openssl \
             /usr/local/opt/openssl/bin/openssl; do
        [ -x "$p" ] && echo "$p" && return 0
    done
    echo "openssl"
}

create_jwt_secret() {
    local ns="$1"
    if oc -n "$ns" get secret openshell-jwt-keys &>/dev/null; then
        info "Secret 'openshell-jwt-keys' already exists, skipping"
        return 0
    fi
    info "Generating Ed25519 JWT signing keypair..."
    local OPENSSL tmpdir kid
    OPENSSL=$(_find_openssl)
    info "Using OpenSSL: $($OPENSSL version)"
    tmpdir=$(mktemp -d)
    $OPENSSL genpkey -algorithm Ed25519 -out "$tmpdir/signing.pem" 2>/dev/null
    $OPENSSL pkey -in "$tmpdir/signing.pem" -pubout -out "$tmpdir/public.pem" 2>/dev/null
    kid=$($OPENSSL pkey -in "$tmpdir/signing.pem" -pubout -outform DER 2>/dev/null \
        | $OPENSSL dgst -sha256 -binary | $OPENSSL base64 -A | tr '+/' '-_' | tr -d '=')
    echo "$kid" > "$tmpdir/kid.txt"
    oc -n "$ns" create secret generic openshell-jwt-keys \
        --from-file=signing.pem="$tmpdir/signing.pem" \
        --from-file=public.pem="$tmpdir/public.pem" \
        --from-file=kid="$tmpdir/kid.txt"
    rm -rf "$tmpdir"
    info "JWT signing secret created"
}

install_agent_sandbox_crd() {
    step "Install Agent Sandbox CRD and controller"
    oc apply -f \
        https://github.com/kubernetes-sigs/agent-sandbox/releases/latest/download/sandbox.yaml
    wait_for_pod_ready "agent-sandbox-system" "control-plane=controller-manager" 120
}

create_openshell_namespace() {
    local ns="$1"
    step "Create namespace $ns"
    oc create ns "$ns" --dry-run=client -o yaml | oc apply -f -
}

grant_privileged_scc() {
    local ns="$1"
    step "Grant privileged SCC to openshell-sandbox SA"
    oc adm policy add-scc-to-user privileged -z openshell-sandbox -n "$ns"
}
