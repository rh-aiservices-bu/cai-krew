# Hermes on OpenShell

Runs the [Hermes agent](https://github.com/RHRolun/hermes-agent) inside an [OpenShell](https://github.com/nvidia/openshell) sandbox on OpenShift. The sandbox provides an isolated execution environment for the agent with persistent storage under `/sandbox/`.

## Architecture

```
OpenShell Gateway (StatefulSet)
  └── Hermes sandbox (agent-sandbox CRD)
        ├── quay.io/rlundber/hermes-openshell:latest
        ├── config: /sandbox/.hermes/config.yaml
        ├── soul:   /sandbox/.hermes/SOUL.md
        └── env:    /sandbox/.sandbox-init.sh  (sourced on login)
```

The gateway and sandbox run in the same namespace as the rest of the cai-crew stack (`cai-crew`), so they can reach mem0, nomic-embed, and other services by short DNS name.

## Prerequisites

- `oc` CLI, logged in with cluster-admin
- The `cai-crew` namespace already exists

## Step 1 — Apply RBAC and scripts

```bash
# Replace PLACEHOLDER_NAMESPACE with the namespace where you run the Jobs
sed 's/PLACEHOLDER_NAMESPACE/cai-crew/' \
  manifests/sa-installer.yaml | oc apply -f -

oc apply -f manifests/configmap-scripts.yaml -n cai-crew
```

`sa-installer.yaml` creates a `openshell-installer` ServiceAccount with a `cluster-admin` ClusterRoleBinding (required to install CRDs, create namespaces, and grant SCCs).

## Step 2 — Install the OpenShell gateway

```bash
oc apply -f manifests/job-install-gateway.yaml -n cai-crew
oc logs -f job/openshell-install-gateway -n cai-crew
```

This Job:
1. Installs the `agent-sandbox` CRD and controller
2. Grants the privileged SCC to the `openshell-sandbox` ServiceAccount
3. Generates an Ed25519 JWT signing keypair and stores it as `openshell-jwt-keys`
4. Installs the OpenShell Helm chart into `cai-crew`
5. Exposes the gateway via an OpenShift Route

At the end of the logs you will see the gateway URL:

```
 URL: http://openshell-gw-cai-crew.apps.<cluster>
```

To verify the gateway pod is running:

```bash
oc get pods -n cai-crew -l app.kubernetes.io/name=openshell
```

## Step 3 — Fill in credentials

Copy the template and fill in values:

```bash
cp manifests/secret-sandbox-credentials.yaml /tmp/my-credentials.yaml
# edit /tmp/my-credentials.yaml
oc apply -f /tmp/my-credentials.yaml -n cai-crew
```

Required fields:

| Field | Description |
|---|---|
| `OPENAI_BASE_URL` | LiteLLM or OpenAI-compatible endpoint |
| `OPENAI_API_KEY` | API key for the above |
| `OPENAI_MODEL` | Model name, e.g. `Qwen3.6-35B-A3B` |
| `SLACK_BOT_TOKEN` | Slack bot token (xoxb-...) |
| `SLACK_APP_TOKEN` | Slack app-level token (xapp-...) |
| `MEM0_URL` | mem0 server URL (default points to cluster-internal `mem0-server:8000`) |

For Mattermost instead of Slack, leave the Slack fields empty and fill in `MATTERMOST_URL`, `MATTERMOST_TOKEN`, `MATTERMOST_TEAM`. Update `PLATFORM` in `job-setup-sandbox.yaml` to `mattermost`.

## Step 4 — Set up the Hermes sandbox

```bash
oc apply -f manifests/job-setup-sandbox.yaml -n cai-crew
oc logs -f job/hermes-setup-sandbox -n cai-crew
```

This Job:
1. Downloads the `openshell` CLI
2. Registers the LLM provider with the gateway
3. Creates the Hermes sandbox from `quay.io/rlundber/hermes-openshell:latest`
4. Uploads `config.yaml` (rendered from the template with credentials substituted)
5. Uploads `SOUL.md` (agent persona)
6. Writes `/sandbox/.sandbox-init.sh` with all environment variables and sources it on login

## Step 5 — Connect and start Hermes

From any machine with the `openshell` CLI and the gateway registered:

```bash
# Register the gateway (one-time per machine)
openshell gateway add http://openshell-gw-cai-crew.apps.<cluster> --local --name openshift

# Connect to the sandbox
openshell sandbox connect hermes

# Inside the sandbox — start Hermes
hermes
```

Hermes will connect to Slack (or Mattermost) and begin listening.

## Customisation

### Changing the agent persona

Edit `config/SOUL.md`, then re-run the setup Job (it will re-upload the file):

```bash
oc apply -f manifests/configmap-scripts.yaml -n cai-crew
oc delete job hermes-setup-sandbox -n cai-crew
oc apply -f manifests/job-setup-sandbox.yaml -n cai-crew
```

### Changing the Hermes config

Edit `config/hermes-config.yaml.template`, apply the ConfigMap, then re-run the setup Job as above.

### Rebuilding the container image

The image is built from `../Containerfile.openshell` via a BuildConfig in OpenShift pointing at the `main` branch of this repo. Trigger a rebuild after pushing changes:

```bash
oc start-build hermes-openshell -n cai-crew --follow
```

## Teardown

```bash
# Remove the sandbox
openshell sandbox delete hermes

# Uninstall the gateway
helm uninstall openshell -n cai-crew

# Remove the JWT secret
oc delete secret openshell-jwt-keys -n cai-crew

# Remove the Jobs and supporting resources
oc delete job openshell-install-gateway hermes-setup-sandbox -n cai-crew
oc delete sa openshell-installer -n cai-crew
oc delete clusterrolebinding openshell-installer openshell-node-reader
```

To also remove the agent-sandbox CRD and controller:

```bash
oc delete -f https://github.com/kubernetes-sigs/agent-sandbox/releases/latest/download/sandbox.yaml
```
