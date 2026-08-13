# Atlassian OAuth Integration for MCP Gateway

Step-by-step guide to configure per-user Atlassian OAuth tokens for Jira tool access through the MCP Gateway.

## Why

The MCP Gateway proxies tool calls to the [Atlassian MCP Server](https://github.com/atlassian/atlassian-mcp-server) at `mcp.atlassian.com`. Atlassian requires OAuth 2.0 Bearer tokens on the `/v1/mcp` endpoint. Each user needs their own token so they only see their own Jira issues, projects, and permissions.

## What Failed (and Why)

We tried three approaches before finding one that works with managed Atlassian organizations:

### Approach 1: API Tokens (PATs) - Blocked

The `/v1/mcp/authv2` endpoint accepts API tokens as `Basic base64(email:token)`. But managed Atlassian organizations can block API token access unless the org admin explicitly enables it under **Security - API token controls**.

Error: `"You don't have permission to connect via API token"`

### Approach 2: Custom OAuth 2.0 (3LO) App - Blocked

We registered a custom OAuth app at [developer.atlassian.com](https://developer.atlassian.com/console/myapps/), configured Keycloak as an OIDC Identity Provider to broker the Atlassian identity, and tried the consent flow.

Error: Consent page showed `"There's nothing here"` - the managed Atlassian org blocks unapproved third-party OAuth apps on the consent page. The org admin needs to approve the app first.

### Approach 3: MCP Dynamic Registration + auth-ui Redirect - Blocked

Atlassian's MCP server publishes its own OAuth metadata at `mcp.atlassian.com/.well-known/oauth-authorization-server` with dynamic client registration. We registered a client with the auth-ui as redirect URI and the consent page loaded this time (progress!) - but the managed org blocks external redirect URIs.

Error: `"Your organization admin must authorize access from this redirect URL to this site"` with a link to an allowlist guide. The org admin needs to add external redirect URLs to an allowlist.

### What Works: Localhost Redirect (Option 4)

The key insight: **Claude Code uses the exact same Atlassian MCP OAuth flow** (`mcp.atlassian.com/v1/authorize`) and it works without org-admin approval. The difference is Claude Code uses a `http://localhost` redirect URI, which managed Atlassian orgs allowlist by default.

So we:
1. Register a dynamic client with `http://localhost:18923/callback` as redirect
2. Run the OAuth flow locally from the user's laptop (one-time setup)
3. Store the tokens in Keycloak as user attributes
4. Authorino reads the tokens from userinfo and injects them on every Jira tool call

No org-admin approval needed.

## Architecture

```
Per-user first-time setup (one-time, from laptop):
  link-atlassian.py
    -> starts localhost:18923 HTTP server
    -> opens browser to mcp.atlassian.com/v1/authorize (PKCE)
    -> user logs in via Atlassian (Google SSO / corporate SSO)
    -> Atlassian redirects to localhost:18923/callback with auth code
    -> script exchanges code for access_token + refresh_token
    -> script stores both as Keycloak user attributes

Runtime (every Jira tool call):
  Hermes / Client (Keycloak JWT)
    -> MCP Gateway broker (mcp listener)
      -> ext_proc sets x-mcp-toolname, forwards to mcps listener
        -> Authorino:
            1. Validates Keycloak JWT
            2. Fetches userinfo (includes atlassian_oauth_token)
            3. Injects Authorization: Bearer <atlassian_oauth_token>
          -> URLRewrite: host -> mcp.atlassian.com
          -> RequestHeaderModifier: Accept -> application/json, text/event-stream
          -> TLS origination (DestinationRule)
            -> mcp.atlassian.com:443/v1/mcp
```

## Prerequisites

- MCP Gateway installed and running with the `mcps` listener
- Keycloak `mcp` realm configured with the `mcp-gateway` public client
- Istio as the Gateway API provider (ServiceEntry + DestinationRule support)
- Python 3 on the user's laptop (for the link script)
- The Atlassian MCP manifests deployed (ServiceEntry, DestinationRule, HTTPRoute, AuthPolicy)

## Step 1: Register an Atlassian MCP OAuth Client

Atlassian's MCP server supports [RFC 7591 Dynamic Client Registration](https://datatracker.ietf.org/doc/html/rfc7591). You don't need to create an app in the Atlassian Developer Console - just POST to the registration endpoint.

```bash
curl -X POST https://cf.mcp.atlassian.com/v1/register \
  -H "Content-Type: application/json" \
  -d '{
    "redirect_uris": ["http://localhost:18923/callback"],
    "token_endpoint_auth_method": "none",
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "client_name": "mcp-gateway-local-linker"
  }'
```

Save the `client_id` from the response. This is a **public client** (PKCE only, no client_secret).

> **Why localhost?** Managed Atlassian organizations allowlist `localhost` redirect URIs by default. External redirect URIs (like `https://auth-ui.apps.example.com/callback`) require org-admin approval via an allowlist. Localhost bypasses that restriction entirely - same approach Claude Code uses.

The OAuth metadata is published at:
```
GET https://mcp.atlassian.com/.well-known/oauth-authorization-server
```

Key endpoints:
| Endpoint | URL |
|----------|-----|
| Authorization | `https://mcp.atlassian.com/v1/authorize` |
| Token | `https://cf.mcp.atlassian.com/v1/token` |
| Registration | `https://cf.mcp.atlassian.com/v1/register` |

## Step 2: Add Keycloak User Attributes and Protocol Mappers

The OAuth tokens are stored as Keycloak user attributes. Authorino reads them from the userinfo endpoint, so we need protocol mappers that expose them.

### 2.1 Declare the Attributes in the User Profile

```bash
export KEYCLOAK_URL="https://<KEYCLOAK_HOST>"
ADMIN_TOKEN=$(curl -sk -X POST "$KEYCLOAK_URL/realms/master/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=admin-cli&username=<admin>&password=<admin-pass>" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Get current profile
PROFILE=$(curl -sk "$KEYCLOAK_URL/admin/realms/mcp/users/profile" \
  -H "Authorization: Bearer $ADMIN_TOKEN")

# Add atlassian_oauth_token and atlassian_oauth_refresh to the attributes array
# (edit the JSON and PUT back)
```

Or add them manually in the admin console: **Realm Settings - User Profile - Add attribute**.

### 2.2 Add Protocol Mappers to the mcp-gateway Client

Go to the Keycloak admin console: **Clients - mcp-gateway - Client scopes - mcp-gateway-dedicated - Add mapper - By configuration - User Attribute**.

Create two mappers:

| Mapper Name | User Attribute | Token Claim Name | Add to userinfo |
|-------------|---------------|-----------------|-----------------|
| `atlassian-oauth-token-userinfo` | `atlassian_oauth_token` | `atlassian_oauth_token` | ON |
| `atlassian-oauth-refresh-userinfo` | `atlassian_oauth_refresh` | `atlassian_oauth_refresh` | ON |

> These mappers expose the stored tokens via the userinfo endpoint. Authorino fetches userinfo on every tool call and reads `atlassian_oauth_token` from the response.

## Step 3: Link a User's Atlassian Account

Each user runs `link-atlassian.py` once from their laptop. The script:

1. Opens the browser to `mcp.atlassian.com/v1/authorize` with PKCE
2. Starts a local HTTP server on `localhost:18923`
3. User logs in via Atlassian (Google SSO / corporate SSO works)
4. Atlassian redirects to `localhost:18923/callback` with an authorization code
5. Script exchanges the code for `access_token` + `refresh_token`
6. Script stores both in Keycloak as user attributes

```bash
python3 link-atlassian.py \
  --keycloak-user <username> \
  --keycloak-url "https://<KEYCLOAK_HOST>" \
  --keycloak-admin <admin-user> \
  --keycloak-admin-pass "<admin-password>"
```

Or with environment variables:

```bash
export KEYCLOAK_URL="https://<KEYCLOAK_HOST>"
export KEYCLOAK_ADMIN_PASS="<admin-password>"
python3 link-atlassian.py --keycloak-user <username>
```

The browser will open with the Atlassian MCP consent page showing:
- **Name**: mcp-gateway-local-linker
- **Redirect URIs**: http://localhost:18923/callback
- **Apps**: Jira (checked), Confluence (checked), Compass (optional)

Click **Approve**, then authenticate with your Atlassian account.

After the script finishes, the user's Keycloak profile will have:
- `atlassian_oauth_token` - the access token (~8h expiry)
- `atlassian_oauth_refresh` - the refresh token (long-lived, no documented expiry)

### Important: Update the link-atlassian.py Client ID

The `ATLASSIAN_CLIENT_ID` constant in `link-atlassian.py` must match the `client_id` from Step 1. If you registered a new client, update the script.

## Step 4: Update the AuthPolicy

Change the AuthPolicy to read `atlassian_oauth_token` (OAuth) instead of `atlassian_pat` (PAT) from userinfo:

```yaml
# authpolicy-atlassian.yaml
response:
  success:
    headers:
      authorization:
        plain:
          selector: "Bearer {auth.metadata.atlasuserinfo.atlassian_oauth_token}"
        when:
        - predicate: "'atlasuserinfo' in auth.metadata && auth.metadata.atlasuserinfo.atlassian_oauth_token != ''"
```

The metadata section stays the same - it fetches from the Keycloak userinfo endpoint. The only change is the attribute name in the response selector and predicate.

```bash
oc apply -f authpolicy-atlassian.yaml
```

## Step 5: Fix the Accept Header (Required)

The Atlassian MCP server requires `Accept: application/json, text/event-stream` on all requests. The MCP Gateway router only sends `Accept: application/json`, which causes Atlassian to return HTTP 406:

```json
{"error":{"code":-32000,"message":"Not Acceptable: Client must accept both application/json and text/event-stream"}}
```

Fix this by adding a `RequestHeaderModifier` to the HTTPRoute:

```yaml
# httproute-atlassian.yaml
spec:
  rules:
  - matches:
    - path:
        type: PathPrefix
        value: /v1/mcp
    filters:
    - type: URLRewrite
      urlRewrite:
        hostname: mcp.atlassian.com
    - type: RequestHeaderModifier
      requestHeaderModifier:
        set:
        - name: Accept
          value: "application/json, text/event-stream"
    backendRefs:
    - name: mcp.atlassian.com
      kind: Hostname
      group: networking.istio.io
      port: 443
```

```bash
oc apply -f httproute-atlassian.yaml
```

> Without this fix, the broker can discover tools (its own initialization handles Accept correctly) but per-user tool calls fail with 406 because the ext_proc/router path only sends `Accept: application/json`.

## Step 6: Update the Broker credentialRef Secret

The broker needs a valid OAuth Bearer token for its own session initialization (tool discovery at startup). Update the `atlassian-mcp-token` secret with an OAuth token:

```bash
# Get a user's OAuth token from Keycloak (after they ran link-atlassian.py)
ATLAS_TOKEN=$(curl -sk "$KEYCLOAK_URL/admin/realms/mcp/users?username=<linked-user>&exact=true" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['attributes']['atlassian_oauth_token'][0])")

oc create secret generic atlassian-mcp-token -n mcp-test \
  --from-literal=token="Bearer $ATLAS_TOKEN" \
  --dry-run=client -o yaml | oc apply -f -

# Add the required label
oc label secret atlassian-mcp-token -n mcp-test mcp.kuadrant.io/secret=true
```

Then re-create the MCPServerRegistration to pick up the new secret:

```bash
oc delete mcpserverregistration atlassian-mcp-server -n mcp-test
oc apply -f mcpsr-atlassian.yaml

# Wait for it to discover tools
oc get mcpserverregistration atlassian-mcp-server -n mcp-test -w
# Should show: Ready=True, TOOLS=19
```

> The broker's credentialRef token will also expire (~8h). You need a token refresh mechanism to keep it alive (see Token Refresh section below).

## Step 7: Verify End-to-End

### 7.1 Check the MCPServerRegistration

```bash
oc get mcpserverregistration -n mcp-test atlassian-mcp-server
```

Should show `Ready=True` with 19 tools (3 Teamwork Graph + 16 Jira/Confluence tools).

### 7.2 Test a Jira Tool Call

```bash
export KEYCLOAK_URL="https://<KEYCLOAK_HOST>"
export MCP_HOST="<MCP_GATEWAY_HOST>"

# Get a token for the linked user
TOKEN=$(curl -sk -X POST "$KEYCLOAK_URL/realms/mcp/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=mcp-gateway&username=<user>&password=<pass>&scope=openid" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Initialize MCP session
SESSION=$(curl -s -D- -X POST "http://$MCP_HOST/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}' \
  | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')

# Send initialized notification
curl -s -X POST "http://$MCP_HOST/mcp" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "mcp-session-id: $SESSION" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' > /dev/null

# Get accessible Atlassian resources (to find cloudId)
curl -s -X POST "http://$MCP_HOST/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "mcp-session-id: $SESSION" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"jira_getAccessibleAtlassianResources","arguments":{}}}'
```

Should return a list of accessible Atlassian sites with their `cloudId`.

### 7.3 Search Jira Issues

```bash
curl -s -X POST "http://$MCP_HOST/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "mcp-session-id: $SESSION" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "jira_searchJiraIssuesUsingJql",
      "arguments": {
        "cloudId": "<cloudId-from-above>",
        "jql": "assignee = currentUser() ORDER BY updated DESC",
        "maxResults": 3
      }
    }
  }'
```

Should return Jira issues assigned to the user.

## Token Refresh

Atlassian access tokens expire in ~8 hours. The refresh token is long-lived. To keep the access token alive, you need a CronJob that:

1. Lists Keycloak users with `atlassian_oauth_refresh` set
2. For each user, calls `cf.mcp.atlassian.com/v1/token` with `grant_type=refresh_token`
3. Updates the user's `atlassian_oauth_token` (and `atlassian_oauth_refresh` if rotated) in Keycloak
4. Also updates the broker's `atlassian-mcp-token` secret with a fresh token

```bash
# Manual refresh example:
curl -X POST https://cf.mcp.atlassian.com/v1/token \
  -H "User-Agent: mcp-gateway-refresher/1.0" \
  -d "grant_type=refresh_token" \
  -d "refresh_token=<stored_atlassian_oauth_refresh>" \
  -d "client_id=<ATLASSIAN_CLIENT_ID>"
# Returns: {"access_token": "...", "refresh_token": "...", "expires_in": 3600}
```

> **Important**: The `cf.mcp.atlassian.com` token endpoint is behind Cloudflare. Requests with a default Python `urllib` User-Agent get blocked with error 1010. Always set a custom `User-Agent` header (e.g. `mcp-gateway-refresher/1.0`).

## Troubleshooting

### "Your organization admin must authorize access from this redirect URL"

You're using an external redirect URI (not localhost). Managed Atlassian orgs only allowlist `localhost` by default. Use `link-atlassian.py` with the localhost redirect instead. If you need a web-based flow, ask your org admin to add the redirect URL to the allowlist.

### "Not Acceptable: Client must accept both application/json and text/event-stream"

The `RequestHeaderModifier` in the HTTPRoute is missing. Apply the updated `httproute-atlassian.yaml` with the `Accept` header set to `application/json, text/event-stream`.

### "failed to create session for mcp server: server returned 4xx"

The broker's per-user session initialization failed. Common causes:
- **Missing Accept header**: See the fix above
- **Expired token**: The user's `atlassian_oauth_token` expired. Run the token refresh or re-run `link-atlassian.py`
- **Cloudflare 1010**: The request was blocked by Cloudflare bot protection. This usually affects programmatic requests with a default `Python-urllib` User-Agent - not relevant for the Gateway (it uses `mcp-router`)

### "credential secret atlassian-mcp-token is missing required label"

Add the label:
```bash
oc label secret atlassian-mcp-token -n mcp-test mcp.kuadrant.io/secret=true
```

### Token exchange returns HTTP 403 / "error code: 1010"

Cloudflare is blocking the request. Set a custom `User-Agent` header. The `link-atlassian.py` script already handles this.

### Brute force lockout on Keycloak user

If you test with wrong passwords, Keycloak's brute force protection locks the account. Clear it:
```bash
curl -sk -X DELETE "$KEYCLOAK_URL/admin/realms/mcp/attack-detection/brute-force/users/<user-id>" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

## Files

| File | Purpose |
|------|---------|
| `authpolicy-atlassian.yaml` | AuthPolicy - reads `atlassian_oauth_token` from userinfo, injects as Bearer header |
| `httproute-atlassian.yaml` | HTTPRoute - URL rewrite to `mcp.atlassian.com`, Accept header fix |
| `mcpsr-atlassian.yaml` | MCPServerRegistration - registers Atlassian MCP server, path `/v1/mcp` |
| `serviceentry.yaml` | Istio ServiceEntry for `mcp.atlassian.com` |
| `destinationrule.yaml` | TLS origination with SNI |
| `secret.yaml.tmpl` | Broker credentialRef secret template |
| `link-atlassian.py` | Local OAuth PKCE flow - links a user's Atlassian account to Keycloak |

## Related

- [Atlassian MCP Server](https://github.com/atlassian/atlassian-mcp-server) - Upstream project
- [Atlassian MCP OAuth Metadata](https://mcp.atlassian.com/.well-known/oauth-authorization-server) - Dynamic registration and token endpoints
- [RFC 7591 - Dynamic Client Registration](https://datatracker.ietf.org/doc/html/rfc7591)
