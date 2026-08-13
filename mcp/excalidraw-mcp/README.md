# Excalidraw MCP Server - MCP Gateway Backend

Deploys [excalidraw/excalidraw-mcp](https://github.com/excalidraw/excalidraw-mcp) as an in-cluster backend MCP server registered with the MCP Gateway. Provides 5 tools for generating architecture diagrams and exporting them as shareable excalidraw.com links.

Tools: `excalidraw_create_view`, `excalidraw_read_me`, `excalidraw_export_to_excalidraw`, `excalidraw_read_checkpoint`, `excalidraw_save_checkpoint`.

Unlike gitea-mcp and jira-mcp (which use SSE transport), this server uses Streamable HTTP transport natively. The MCP Gateway v0.7.1 router handles Streamable HTTP directly - no supergateway wrapper needed. This also means it requires **two AuthPolicies** (one per gateway listener) instead of the single per-route AuthPolicy used by the other servers. See [Auth Model](#auth-model) for details.

## Prerequisites

- MCP Gateway installed and running (RHCL 1.4, v0.7.1+ with router support)
- `mcp-test` namespace exists
- Keycloak `mcp` realm configured with the `mcp-gateway` client
- Container image built and pushed (see [Container Image](#container-image))
- No external service credentials needed (unlike gitea/jira)

## Auth Model

The MCP Gateway has two listeners: `mcp` (public, external traffic) and `mcps` (internal, router-to-upstream traffic). Each listener evaluates AuthPolicies independently. Excalidraw needs a separate policy for each because the gateway router creates its own HTTP requests to upstream servers through the envoy proxy - it forwards `x-mcp-toolname` and `x-mcp-servername` headers but does NOT forward the client's JWT token.

### Internal AuthPolicy (`authpolicy-excalidraw.yaml`)

Targets the `excalidraw-mcp-server-route` HTTPRoute on the `mcps` listener. Uses anonymous-only authentication with no authorization rules.

Why anonymous-only: the router's upstream requests (session creation, tool forwarding) carry `x-mcp-toolname` headers but no `Authorization` header. If this policy required JWT when `x-mcp-toolname` is present (like gitea-mcp's policy does for credential injection), the router's requests would fail with `401 authorization required`. Auth is already verified on the public listener - the internal path does not need to re-check it.

### Public Gateway Route AuthPolicy (`authpolicy-gateway-route.yaml`)

Targets the `mcp-gateway-route` HTTPRoute on the `mcp` (public) listener. Lives in the `mcp-gateway` namespace because it targets a route in that namespace.

Rules:
- **No `x-mcp-toolname` header** (session init, tool listing): anonymous auth, no authorization check
- **`x-mcp-toolname` header present** (tool calls): Keycloak JWT validation + `tool-access-check` authorization that verifies `tool:<toolname>` exists in the user's `resource_access` roles for the target server

Why this is needed: the MCP Gateway has a default `mcp-auth-policy` at the gateway level that requires Keycloak JWT for all requests. This per-route policy overrides those defaults to allow anonymous for session initialization while still enforcing JWT + role-based access for tool calls.

**Note**: update the `issuerUrl` in this file to match your Keycloak instance.

### Keycloak Client Roles

Create client roles under the `mcp-test/excalidraw-mcp-server` client scope in the `mcp-gateway` Keycloak client:

- `tool:create_view`
- `tool:read_me`
- `tool:export_to_excalidraw`
- `tool:read_checkpoint`
- `tool:save_checkpoint`

Assign these roles to:
1. The **service account user** for `mcp-gateway` (used by `client_credentials` grants from Hermes/other clients)
2. Any **human users** who need direct tool access

Role names must match the tool names exactly. The gateway adds the `excalidraw_` prefix to tool names, but the `x-mcp-toolname` header (used by the auth check) contains the unprefixed name.

## Container Image

Built from `registry.access.redhat.com/ubi9/nodejs-20`. The Containerfile clones the upstream repo, builds with pnpm, and applies two patches to `dist/index.js`:

1. **Accept header patch** (sed) - The MCP SDK validates that incoming requests include `Accept: application/json, text/event-stream`, but the MCP Gateway router does not send this header. Without the patch, the server returns 406 Not Acceptable.

2. **Export element conversion patch** (`patch-export-convert.mjs`) - The `export_to_excalidraw` tool expects full Excalidraw scene JSON (produced by `serializeAsJSON()` in the browser widget). When LLM clients like Hermes call the tool directly through the MCP Gateway, they pass raw shorthand elements from the checkpoint - just a bare array like `[{type: "rectangle", x: 100, label: "foo"}]`. Excalidraw.com can't render that because it expects `{type: "excalidraw", version: 2, elements: [...full elements...]}` with all required properties (`strokeColor`, `seed`, `roughness`, etc.). The browser widget normally does this conversion client-side via `convertToExcalidrawElements()` + `serializeAsJSON()` from `@excalidraw/excalidraw`, but that library requires React/DOM and can't run in Node.js. This patch adds a lightweight server-side conversion that detects raw input, adds default Excalidraw properties, converts `label` to bound text elements, and wraps everything in a valid scene JSON structure.

Current image: `quay.io/rcarrata/excalidraw-mcp-server:v5`

To rebuild:

```bash
podman build -t quay.io/<your-user>/excalidraw-mcp-server:v5 .
podman push quay.io/<your-user>/excalidraw-mcp-server:v5
```

Update the image reference in `excalidraw-mcp-server/deployment.yaml` if using a different registry.

## Deploy

1. Apply all resources (Deployment, Service, HTTPRoute, MCPServerRegistration, both AuthPolicies):

```bash
oc apply -k .
```

2. Wait for the pod to be ready:

```bash
oc wait pod -l app=excalidraw-mcp-server -n mcp-test --for=condition=Ready --timeout=120s
```

## Verify

Check the MCPServerRegistration status:

```bash
oc get mcpserverregistration -n mcp-test
```

The `excalidraw-mcp-server` entry should show `Ready=True`. All tools will be prefixed with `excalidraw_`.

Check both AuthPolicies are enforced:

```bash
oc get authpolicy -n mcp-test
oc get authpolicy -n mcp-gateway
```

Both `excalidraw-mcp-authz-policy` and `mcp-gateway-route-authz-policy` should show `ENFORCED=True`.

End-to-end test (replace the token endpoint and gateway URL for your cluster):

```bash
TOKEN=$(curl -sk "https://<KEYCLOAK_HOST>/realms/mcp/protocol/openid-connect/token" \
  -d "grant_type=client_credentials" \
  -d "client_id=mcp-gateway" \
  -d "client_secret=<your-secret>" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

SESSION_ID=$(curl -s -D - -X POST "http://mcp.apps.<cluster>/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-protocol-version: 2025-11-25" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
  | grep -i mcp-session-id | tr -d '\r' | awk '{print $2}')

curl -s -X POST "http://mcp.apps.<cluster>/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-protocol-version: 2025-11-25" \
  -H "mcp-session-id: $SESSION_ID" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"excalidraw_read_me","arguments":{}}}'
```

Should return the Excalidraw element format reference.

## Hermes (Slack AI Agent) Integration

The excalidraw tools are consumed by Hermes through the MCP Gateway - Hermes connects to the public gateway endpoint, not directly to the excalidraw pod. The relevant section in the Hermes config:

```yaml
mcp_servers:
  gateway:
    url: "http://mcp.apps.<cluster>/mcp"
    auth: oauth
    oauth:
       client_id: mcp-gateway
       client_secret: <your-client-secret>
    keepalive_interval: 60
    timeout: 120
```

Configuration notes:

- **`timeout: 120`** - MCP tool call timeout in seconds. Excalidraw tools like `create_view` and `export_to_excalidraw` can take 10-30s depending on diagram complexity. The 120s value gives enough headroom. If tool calls time out, the router may still be creating a session with the upstream server (first call after a reconnect is slower).
- **`keepalive_interval: 60`** - Sends a ping every 60s to keep the MCP session alive. Without this, the Streamable HTTP connection can be closed by intermediate proxies (envoy idle timeout, OpenShift route timeout). The MCP SDK v1.26.0 has a known issue where the GET SSE stream disconnects after ~5 min, triggering a `TaskGroup` crash and reconnection cycle. The keepalive helps but does not fully prevent this.
- **Token refresh**: Hermes uses a `client_credentials` OAuth grant against the Keycloak token endpoint. A CronJob refreshes the token every 20 minutes (before expiry). The MCP SDK will log `Token refresh failed: 400` warnings because it tries `refresh_token` grant on `client_credentials` tokens (which have no refresh token) - this is a warning, not an error. The CronJob handles the actual refresh.
- The service account user used by the CronJob must have the excalidraw tool roles assigned (see [Keycloak Client Roles](#keycloak-client-roles)).

## How It Works

```
Session init (no tool call):
  Client -> mcp listener (public) -> AuthPolicy (anonymous) -> Router
    -> mcps listener (internal) -> AuthPolicy (anonymous) -> excalidraw-mcp-server:8080/mcp

Tool call (with JWT):
  Client -> mcp listener (public) -> AuthPolicy (keycloak JWT + tool-access-check)
    -> Router -> mcps listener (internal) -> AuthPolicy (anonymous)
    -> excalidraw-mcp-server:8080/mcp
```

Key resources:

| Resource | File | Purpose |
|---|---|---|
| Deployment + Service | `excalidraw-mcp-server/` | Runs the MCP server pod on port 8080 |
| HTTPRoute | `httproute-excalidraw.yaml` | Routes `excalidraw-mcp-server.mcp.local` on `mcps` listener to the service |
| MCPServerRegistration | `mcpsr-excalidraw.yaml` | Registers server with gateway, prefix `excalidraw_`, path `/mcp` |
| AuthPolicy (internal) | `authpolicy-excalidraw.yaml` | Anonymous-only on internal route (router traffic) |
| AuthPolicy (gateway) | `authpolicy-gateway-route.yaml` | JWT + tool-access-check on public route |

## Cleanup

```bash
oc delete -k .
```
