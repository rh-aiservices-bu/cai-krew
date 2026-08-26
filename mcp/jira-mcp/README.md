# Atlassian (Jira) MCP Server — MCP Gateway Backend

Registers the cloud-hosted [Atlassian MCP Server](https://mcp.atlassian.com) (`mcp.atlassian.com`) as a backend with the MCP Gateway. Provides tools for interacting with Jira, Confluence, and Teamwork Graph through the MCP protocol.

Unlike the Gitea MCP server (which deploys a pod), this uses Istio routing to proxy requests to Atlassian's external endpoint with TLS origination.

---

- MCP Gateway installed and running
- `mcp-test` namespace exists
- Istio as the Gateway API provider (ServiceEntry + DestinationRule support)
- Keycloak `mcp` realm configured with the `mcp-gateway` public client
- Python 3 on the user's laptop (for the link script)

Atlassian MCP supports two authentication endpoints:

This setup uses per-user Atlassian OAuth tokens stored as Keycloak user attributes. When a tool call comes through:

1. Authorino validates the user's Keycloak JWT
2. Authorino fetches the user's profile from the Keycloak userinfo endpoint
3. Authorino reads the `atlassian_oauth_token` attribute from the user's profile
4. Authorino injects `Bearer <atlassian_oauth_token>` as the Authorization header sent to Atlassian

The OAuth tokens are obtained via a localhost PKCE flow (`link-atlassian.py`) that uses Atlassian's MCP Dynamic Client Registration (RFC 7591). This approach works with managed Atlassian orgs that block API tokens and third-party OAuth apps - localhost redirect URIs are allowlisted by default.

The broker also needs an OAuth token for session initialization (tool discovery at startup). This is stored in a Secret via `credentialRef`.

See [atlassian-oauth-setup.md](atlassian-oauth-setup.md) for the full step-by-step setup guide, including what approaches failed and why.

## Deploy

1. Register a dynamic OAuth client (see setup guide Step 1) and update `ATLASSIAN_CLIENT_ID` in `link-atlassian.py`.

2. Link at least one user's Atlassian account:

```bash
python3 link-atlassian.py \
  --keycloak-user <username> \
  --keycloak-url "https://<KEYCLOAK_HOST>" \
  --keycloak-admin <admin-user> \
  --keycloak-admin-pass "<admin-password>"
```

3. Create the broker secret with an OAuth token:

Expected output when org permission is missing: `RESULT: PERMISSION ERROR — auth failed at Atlassian`

---

## Deploy (current API token setup)

1. Set your Atlassian API token (for broker initialization):

```bash
export ATLASSIAN_TOKEN="<oauth-token-from-linked-user>"
envsubst < secret.yaml.tmpl | oc apply -f -
oc label secret atlassian-mcp-token -n mcp-test mcp.kuadrant.io/secret=true
```

4. Apply the ServiceEntry, DestinationRule, HTTPRoute, MCPServerRegistration, and AuthPolicy:

```bash
oc apply -k .
```

5. Verify the registration:

```bash
oc get mcpserverregistration -n mcp-test atlassian-mcp-server
# Should show: Ready=True, discoveredTools: 3
```

4. In the auth-ui portal, paste your Atlassian API token in the "Atlassian API Token" card. The portal stores it as `base64(email:token)` automatically.

---

## Architecture

```
Client -> mcp listener -> Broker -> mcps listener -> Authorino (OAuth token injection)
  -> URLRewrite (host: mcp.atlassian.com)
  -> RequestHeaderModifier (Accept: application/json, text/event-stream)
  -> TLS origination (DestinationRule)
  -> mcp.atlassian.com:443/v1/mcp
```

Key resources:
- **ServiceEntry**: Registers `mcp.atlassian.com` in Istio's service registry
- **DestinationRule**: TLS origination with SNI to mcp.atlassian.com
- **HTTPRoute**: Routes `atlassian.mcp.local` to external service via `kind: Hostname` backend, rewrites Host header, sets Accept header
- **AuthPolicy**: Per-user OAuth token injection from Keycloak userinfo
- **link-atlassian.py**: Local PKCE flow to link Atlassian accounts to Keycloak

---

## Cleanup

```bash
oc delete -k .
oc delete secret atlassian-mcp-token -n mcp-test
```
