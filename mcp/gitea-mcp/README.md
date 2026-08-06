# Gitea MCP Server - MCP Gateway Backend

Deploys [gitea/gitea-mcp-server](https://hub.docker.com/r/gitea/gitea-mcp-server) as a backend MCP server registered with the MCP Gateway. Provides 47 tools for interacting with a Gitea instance (repositories, issues, PRs, branches, actions, wiki, etc.).

## Prerequisites

- MCP Gateway installed and running (phases 1-4 of mcp-gateway-guide)
- `mcp-test` namespace exists
- A Gitea instance accessible from the cluster
- A Gitea personal access token with appropriate permissions

## Deploy

1. Set your Gitea credentials:

```bash
export GITEA_HOST="https://your-gitea-instance.com"
export GITEA_ACCESS_TOKEN="your-personal-access-token"
```

2. Create the secret:

```bash
envsubst < secret.yaml.tmpl | oc apply -f -
```

3. Apply the deployment, service, HTTPRoute, and MCPServerRegistration:

```bash
oc apply -k .
```

4. Wait for the pod to be ready:

```bash
oc wait pod -l app=gitea-mcp-server -n mcp-test --for=condition=Ready --timeout=120s
```

## Verify

Check the MCPServerRegistration status:

```bash
oc get mcpserverregistration -n mcp-test
```

The `gitea-mcp-server` entry should show `Ready=True`. All tools will be prefixed with `gitea_` (e.g. `gitea_list_repositories`, `gitea_create_issue`).

## Cleanup

```bash
oc delete -k .
oc delete secret gitea-mcp-server -n mcp-test
```
