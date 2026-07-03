# SDXL Image Generation Plugin

A Hermes `ImageGenProvider` plugin that routes `image_generate` tool calls to a
self-hosted Stable Diffusion XL model via any OpenAI-compatible
`/v1/images/generations` endpoint (LiteLLM, Automatic1111 with `--api`, etc.).

When a user asks Hermes to generate an image in Slack, Hermes calls
`image_generate`, this plugin calls your SDXL endpoint, saves the result
locally, and the Slack adapter automatically uploads it via `files_upload_v2`.

## Installation

Copy this directory into the Hermes user plugins folder on the pod's PVC:

```
$HERMES_HOME/plugins/image_gen/sdxl/
├── __init__.py
└── plugin.yaml
```

With the default OpenShift setup, `HERMES_HOME` is `/app/.hermes`, which is
mounted from the PVC. You can copy the files using `oc cp`:

```bash
oc cp hermes-plugins/sdxl/. <pod-name>:/app/.hermes/plugins/image_gen/sdxl/
```

## Configuration

### 1. Environment variables (`.env` secret)

Add these to your Hermes `.env` secret:

```env
# Base URL of your LiteLLM instance — same one used for the main model
SDXL_BASE_URL=https://litellm-litemaas.apps.prod.rhoai.rh-aiservices-bu.com/v1

# API key for LiteLLM (same key works if SDXL is on the same LiteLLM instance)
SDXL_API_KEY=sk-your-litellm-key

# Model name exactly as registered in your LiteLLM config
SDXL_MODEL=stable-diffusion-xl-base-1.0

# Optional: increase if SDXL is slow to respond (default: 120s)
# SDXL_TIMEOUT=180
```

To find the right `SDXL_MODEL` value, check your LiteLLM `config.yaml` for
the model name under the `model_list` entry that points at your SDXL deployment.

### 2. Hermes `config.yaml`

Add these two blocks to your Hermes `config.yaml` (e.g. in the `config-secret`):

```yaml
# Point the image_generate tool at the SDXL backend
image_gen:
  provider: sdxl

# Enable the user-installed plugin (user plugins are opt-in)
plugins:
  enabled:
    - image_gen/sdxl
```

## How it works

```
User in Slack: "generate an image of a sunset"
        │
        ▼
image_generate tool   (already in hermes-slack toolset)
        │
        ▼
SDXLImageGenProvider  (this plugin)
  POST /v1/images/generations  →  LiteLLM  →  SDXL on cluster
        │
        ▼
PNG saved to $HERMES_HOME/cache/images/
        │
        ▼
Slack adapter uploads via files_upload_v2
```

## Supported aspect ratios

| Hermes value | SDXL resolution |
|-------------|-----------------|
| `landscape` | 1216×832        |
| `square`    | 1024×1024       |
| `portrait`  | 832×1216        |

## Troubleshooting

- **"SDXL_BASE_URL is not set"** — the env var is missing from `.env`.
- **Connection error** — verify `SDXL_BASE_URL` is reachable from inside the pod: `oc exec <pod> -- curl $SDXL_BASE_URL/models`
- **404 on `/images/generations`** — your LiteLLM version may not support image generation, or the model isn't configured for it. Check LiteLLM docs for `image_generation` model config.
- **Timeout** — SDXL inference can be slow on first request (model loading). Increase `SDXL_TIMEOUT` or wait for the model to warm up.
- **Plugin not loading** — run `hermes plugins list` inside the pod to confirm `image_gen/sdxl` appears. Check `$HERMES_HOME/logs/` for plugin discovery errors.
