# Atlassian (Jira) MCP Server - MCP Gateway Backend

Registers the cloud-hosted [Atlassian MCP Server](https://github.com/atlassian/atlassian-mcp-server) (`mcp.atlassian.com`) as a backend with the MCP Gateway. Provides tools for interacting with Jira, Confluence, Bitbucket Cloud, and JSM through the MCP protocol.

Unlike the Gitea MCP server (which deploys a pod), this uses Istio routing to proxy requests to Atlassian's external endpoint with TLS origination.

## Prerequisites

- MCP Gateway installed and running
- `mcp-test` namespace exists
- Istio as the Gateway API provider (ServiceEntry + DestinationRule support)
- An Atlassian Cloud account with an API token (PAT)
- Keycloak `mcp` realm configured with `atlassian_pat` user attribute (for per-user PAT injection)

## Auth model

This setup uses per-user Atlassian PATs stored as Keycloak user attributes. When a tool call comes through:

1. Authorino validates the user's Keycloak JWT
2. Authorino fetches the user's profile from the Keycloak userinfo endpoint
3. Authorino reads the `atlassian_pat` attribute from the user's profile
4. Authorino injects `Bearer <atlassian_pat>` as the Authorization header sent to Atlassian

The broker also needs a PAT for session initialization (tool discovery at startup). This is stored in a Secret via `credentialRef`.

## Deploy

1. Set your Atlassian PAT (used by the broker for session initialization):

```bash
export ATLASSIAN_PAT="your-atlassian-api-token"
```

2. Create the secret:

```bash
envsubst < secret.yaml.tmpl | oc apply -f -
```

3. Apply the ServiceEntry, DestinationRule, HTTPRoute, MCPServerRegistration, and AuthPolicy:

```bash
oc apply -k .
```

4. Verify the registration:

```bash
oc get mcpserverregistration -n mcp-test
```

The `atlassian-mcp-server` entry should show `Ready=True`. All tools will be prefixed with `jira_`.

## How it works

```
Client -> mcp listener -> Broker -> mcps listener -> Authorino (PAT injection)
  -> URLRewrite (host: mcp.atlassian.com) -> TLS origination (DestinationRule)
  -> mcp.atlassian.com:443/v1/mcp
```

Key resources:
- **ServiceEntry**: Registers `mcp.atlassian.com` in Istio's service registry
- **DestinationRule**: TLS origination with SNI to mcp.atlassian.com
- **HTTPRoute**: Routes `atlassian.mcp.local` to external service via `kind: Hostname` backend, rewrites Host header
- **AuthPolicy**: Per-user PAT injection from Keycloak userinfo

## Cleanup

```bash
oc delete -k .
oc delete secret atlassian-mcp-token -n mcp-test
```
