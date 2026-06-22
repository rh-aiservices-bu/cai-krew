# Mem0 on OpenShift

Self-hosted Mem0 deployment: PostgreSQL + pgvector, API server, and dashboard.

- **API:** `https://mem0-server-<namespace>.apps.<cluster-domain>/`
- **Dashboard:** `https://mem0-dashboard-<namespace>.apps.<cluster-domain>/`
- **Docs (Swagger):** `https://mem0-server-<namespace>.apps.<cluster-domain>/docs`

---

## Before deploying to a new cluster or namespace

Update these values in `mem0-openshift.yaml`:

| What | Where in the YAML |
|---|---|
| LiteLLM endpoint | `mem0-server` Deployment → `OPENAI_BASE_URL` |
| LLM model name | `mem0-server` Deployment → `MEM0_DEFAULT_LLM_MODEL` |
| LiteLLM API keys | `mem0-config` Secret → `OPENAI_API_KEY`, `EMBEDDER_API_KEY` |
| JWT secret | `mem0-config` Secret → `JWT_SECRET` (generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`) |
| Dashboard public URL | `mem0-server` Deployment → `DASHBOARD_URL` |
| API public URL | `mem0-dashboard` Deployment → `NEXT_PUBLIC_API_URL` |

The `<namespace>` string in image references is replaced by `sed` at deploy time — see step 2 below.

---

## Deploy steps

### 1. Grant SCC (requires cluster-admin)

```bash
oc adm policy add-scc-to-user anyuid -z mem0-sa -n <namespace>
```

Required because PostgreSQL runs as root and the dashboard runs as uid 1001.

> Re-run this if the namespace is recreated or you see `forbidden: anyuid not usable by serviceaccount`.

### 2. Apply the manifest

```bash
sed 's/<namespace>/<your-namespace>/g' mem0-openshift.yaml | oc apply -f - -n <your-namespace>
```

### 3. Trigger the builds (first time only)

```bash
oc start-build mem0-server -n <namespace> --follow
oc start-build mem0-dashboard -n <namespace> --follow
```

Server build: ~3 min. Dashboard (Next.js): ~5 min. Wait for both:

```bash
oc rollout status deployment/mem0-server -n <namespace>
oc rollout status deployment/mem0-dashboard -n <namespace>
```

### 4. Configure LLM and embedder (once per fresh database)

> **Skip this step** if redeploying to the same namespace with the existing PVC — the config persists in the database.

The server defaults to OpenAI text-embedding-3-small (1536 dims). We use a 768-dim embedding model, so the vector store must be reconfigured before any memories are written.

```bash
ROUTE=$(oc get route mem0-server -n <namespace> -o jsonpath='{.spec.host}')

curl -X POST https://$ROUTE/configure \
  -H "Content-Type: application/json" \
  -d '{
    "vector_store": {
      "provider": "pgvector",
      "config": {
        "host": "mem0-postgres", "port": 5432,
        "dbname": "mem0", "user": "mem0", "password": "mem0pass",
        "collection_name": "memories", "embedding_model_dims": 768
      }
    },
    "llm": {
      "provider": "openai",
      "config": {
        "model": "<llm-model-name>",
        "api_key": "<OPENAI_API_KEY>",
        "openai_base_url": "<litellm-url>"
      }
    },
    "embedder": {
      "provider": "openai",
      "config": {
        "model": "<embedding-model-name>",
        "api_key": "<EMBEDDER_API_KEY>",
        "openai_base_url": "<litellm-url>",
        "embedding_dims": 768
      }
    }
  }'
```

Drop the memories table so it gets recreated with the correct 768 dims on next use:

```bash
oc exec -n <namespace> deploy/mem0-postgres -- psql -U mem0 -d mem0 -c "DROP TABLE IF EXISTS memories;"
oc rollout restart deployment/mem0-server -n <namespace>
```

**Why this can't be automated in the YAML:** the `/configure` endpoint requires the server to be running. Init containers execute before the main container starts, so they can't call the API. This is a known limitation — a fix would require either a sidecar job or upstream server support for env-var-driven initial config.

### 5. Create the admin account (once per fresh database)

```bash
ROUTE=$(oc get route mem0-server -n <namespace> -o jsonpath='{.spec.host}')

curl -X POST https://$ROUTE/auth/register \
  -H "Content-Type: application/json" \
  -d '{"name": "Admin", "email": "<your-email>", "password": "<your-password>"}'
```

Registration closes automatically after the first admin is created.

---

## Notes

- **`AUTH_DISABLED=true`** is set — API endpoints work without a Bearer token. The dashboard still requires login. Remove this for production and set a proper JWT secret.
- **Config persistence** — LLM/embedder/vector-store config lives in the `mem0_app` postgres database on the PVC. Steps 4 and 5 only need repeating if the PVC is wiped.
- **Alembic migrations** run automatically as an init container on every server pod start.
- **Dashboard 405 on login** — if this happens after a pod restart, do a hard refresh (`Ctrl+Shift+R`). The Next.js bundle is patched at container start with the correct API URL; cached old bundles can cause this.
- **Memories table dimensions** — the `memories` table is created with `vector(768)`. If you change embedding models, drop the table and restart so it's recreated at the new dimension.

---

## API quick reference

```bash
ROUTE=https://mem0-server-<namespace>.apps.<cluster-domain>

# Add a memory
curl -X POST $ROUTE/memories \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "..."}], "user_id": "alice"}'

# Search memories
curl -X POST $ROUTE/search \
  -H "Content-Type: application/json" \
  -d '{"query": "...", "user_id": "alice"}'

# List all memories for a user
curl "$ROUTE/memories?user_id=alice"
```
