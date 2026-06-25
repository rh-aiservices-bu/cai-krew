# memory-overseer

Daily batch job that reviews all mem0 memories and identifies ones that should be
pruned (duplicates, contradictions, outdated facts) or merged.

**v1: report-only.** The job prints a JSON pruning plan to stdout and exits.
No changes are written to mem0. A human reviews the plan and approves changes.
v2 will add Slack interactive approval + automatic apply.

---

## How it works

1. Fetches all personal memories per actor key (`GET /memories?agent_id={actor_key}`)
2. Fetches all team memories (`GET /memories?agent_id=__team__`)
3. Chunks each set into batches of 30 (5-memory overlap between batches)
4. Sends each batch to the LLM with a pruning prompt
5. Collects the plan: `DELETE` and `MERGE` actions with reasons
6. Prints the full JSON report to stdout + a summary line to stderr

Uses two different prompts (mirroring the plugin):
- **Personal**: focused on individual preferences, habits, and experiences
- **Team**: focused on project decisions and technical standards — more conservative
  (won't delete old decisions unless directly superseded)

---

## Env vars

| Var | Required | Description |
|---|---|---|
| `MEM0_URL` | yes | Base URL of the mem0 server |
| `LITELLM_URL` | yes | Base URL of the LiteLLM proxy |
| `OPENAI_API_KEY` | yes | API key for the LiteLLM proxy |
| `MEM0_ACTOR_KEYS` | no | Comma-separated actor keys for personal memories (e.g. `hermes\|alice,hermes\|bob`). If omitted, only team memories are analyzed. |
| `LITELLM_MODEL` | no | LLM model name (default: `Qwen3.6-35B-A3B`) |

---

## Run locally

```bash
pip install -r requirements.txt

MEM0_URL=https://mem0-server-... \
LITELLM_URL=https://litellm-... \
OPENAI_API_KEY=sk-... \
MEM0_ACTOR_KEYS="hermes|alice,hermes|bob" \
python run.py | tee report.json
```

---

## Finding your actor keys

Actor keys are the `agent_id` values stored on personal memories. To discover them:

```bash
# List all agent_ids in the memories table (requires direct Postgres access)
oc exec -n <namespace> deploy/mem0-postgres -- \
  psql -U mem0 -d mem0 -c "SELECT DISTINCT payload->>'agent_id' FROM memories;"
```

---

## Deploy to OpenShift

```bash
# Build image (first time)
oc new-build --binary --name=memory-overseer -n <namespace>
oc start-build memory-overseer --from-dir=. --follow -n <namespace>

# Deploy CronJob (fill in MEM0_ACTOR_KEYS and cluster domain first)
oc apply -f cronjob.yaml -n <namespace>

# Trigger a manual run to test
oc create job --from=cronjob/memory-overseer memory-overseer-test -n <namespace>
oc logs -f job/memory-overseer-test -n <namespace>
```

---

## Output format

```json
{
  "personal": {
    "hermes|alice": {
      "memories_count": 47,
      "plan": [
        {"action": "DELETE", "ids": ["uuid1"], "reason": "duplicate of uuid2"},
        {"action": "MERGE", "ids": ["uuid3", "uuid4"], "new_text": "...", "reason": "..."}
      ]
    }
  },
  "team": {
    "memories_count": 12,
    "plan": []
  },
  "summary": {
    "total_memories": 59,
    "total_deletes": 3,
    "total_merges": 1
  }
}
```

---

## Roadmap

- **v2**: Slack integration — post the plan to a channel, apply changes only after
  explicit approval (interactive buttons or a slash command)
