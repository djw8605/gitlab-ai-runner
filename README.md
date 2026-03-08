# gitlab-ai-runner

A self-hosted GitLab **@openhands** mention automation that launches Kubernetes Jobs to run an AI coding agent backed by a local OpenAI-compatible vLLM endpoint.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Repository Layout](#repository-layout)
3. [Emoji Reaction Behaviour](#emoji-reaction-behaviour)
4. [GitLab Token Permissions](#gitlab-token-permissions)
5. [Kubernetes RBAC](#kubernetes-rbac)
6. [Building and Pushing Images](#building-and-pushing-images)
7. [Deploying to Kubernetes](#deploying-to-kubernetes)
8. [Configuring the GitLab Webhook](#configuring-the-gitlab-webhook)
9. [Supported Commands](#supported-commands)
10. [Environment Variables Reference](#environment-variables-reference)
11. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
GitLab ──webhook──► webhook receiver (FastAPI, always-on Deployment)
                         │
                         ├─ validates secret & event type
                         ├─ parses @openhands command
                         ├─ adds 👀 reaction to triggering comment
                         ├─ creates Kubernetes Job (runner)
                         └─ adds 🚀 reaction after successful job creation

                    Kubernetes Job (ephemeral runner)
                         │
                         ├─ review: fetches MR diff → LLM → posts MR note
                         └─ fix:    clones repo → LLM → commits → pushes
                                    → opens new MR → posts link
```

---

## Repository Layout

```
gitlab-ai-runner/
├── webhook_receiver/
│   ├── __init__.py
│   ├── main.py          # FastAPI app: POST /webhook, GET /healthz
│   ├── k8s.py           # Kubernetes Job creation helper
│   ├── gitlab.py        # GitLab client (reactions, notes, MR creation)
│   ├── models.py        # Pydantic models for webhook payload
│   └── requirements.txt
├── runner/
│   ├── runner.py        # Job entrypoint
│   ├── gitlab.py        # GitLab client (shared logic)
│   ├── llm.py           # OpenAI-compatible vLLM client wrapper
│   ├── workspace.py     # git clone/branch/commit/push + test runner
│   └── requirements.txt
├── Dockerfile.webhook   # Image for the webhook receiver Deployment
├── Dockerfile.runner    # Image for the ephemeral runner Job
├── k8s/
│   ├── serviceaccount.yaml
│   ├── role.yaml
│   ├── rolebinding.yaml
│   ├── webhook-deployment.yaml
│   ├── webhook-service.yaml
│   ├── webhook-ingress.yaml  # optional – disabled by default
│   ├── configmap.yaml
│   └── secrets.yaml          # ⚠️  DO NOT COMMIT with real values
└── README.md
```

---

## Emoji Reaction Behaviour

| Step | Action | Emoji |
|------|--------|-------|
| 1 | Webhook validated; comment starts with `@openhands`; user passes allowlist | 👀 `eyes` |
| 2 | Kubernetes Job created successfully | 🚀 `rocket` |
| 2 (error) | Job creation failed → post failure comment instead | *(no 🚀)* |

Reactions are **idempotent**: if the same webhook fires twice (e.g. because of a GitLab retry), the receiver detects the existing Job and skips creation but still ensures the 🚀 reaction is added.

GitLab API endpoint used:
```
POST /api/v4/projects/:id/{issues|merge_requests}/:iid/notes/:note_id/award_emoji
Body: {"name": "eyes"}  # or "rocket"
```
A `409 Conflict` response (reaction already exists) is treated as success.

---

## GitLab Token Permissions

Create a **dedicated GitLab bot account** and generate a Personal Access Token (PAT) or Project Access Token with the following scopes:

| Scope | Why it is needed |
|-------|-----------------|
| `api` | Create MRs, post notes, add emoji reactions, read MR/issue metadata |
| `read_repository` | Read repository contents and metadata |
| `write_repository` | Push branches with AI-generated commits |

At the **project level**, the bot account must have at least **Developer** role so that it can:
- Read issues and MRs
- Push to non-protected branches
- Create merge requests
- Post comments

> **Tip:** If your branch protection rules require maintainer access to push, grant the bot **Maintainer** role, or create a dedicated unprotected branch prefix (e.g. `ai/*`).

---

## Kubernetes RBAC

The webhook receiver runs under the `openhands-webhook` ServiceAccount.  All RBAC is **namespace-scoped** (not cluster-wide).

| Resource | API Group | Verbs | Reason |
|----------|-----------|-------|--------|
| `jobs` | `batch` | `create, get, list, watch, delete` | Create and inspect runner Jobs; delete is optional but enables cleanup |
| `pods` | *(core)* | `get, list, watch` | Optional: allows the receiver to link to pod logs when diagnosing failures |

Deploy order:
1. `serviceaccount.yaml`
2. `role.yaml`
3. `rolebinding.yaml`

---

## Building and Pushing Images

```bash
# Set your container registry
export REGISTRY=registry.example.com/myorg

# Build and push the webhook receiver
docker build -f Dockerfile.webhook -t $REGISTRY/openhands-webhook:latest .
docker push $REGISTRY/openhands-webhook:latest

# Build and push the runner Job image
docker build -f Dockerfile.runner -t $REGISTRY/openhands-runner:latest .
docker push $REGISTRY/openhands-runner:latest
```

Then update `k8s/configmap.yaml` (JOB_IMAGE) and `k8s/webhook-deployment.yaml` with your actual registry paths.

---

## Deploying to Kubernetes

```bash
# 1. Create the namespace (if it doesn't exist)
kubectl create namespace openhands

# 2. Apply RBAC
kubectl apply -f k8s/serviceaccount.yaml
kubectl apply -f k8s/role.yaml
kubectl apply -f k8s/rolebinding.yaml

# 3. Fill in secrets (see k8s/secrets.yaml for fields) and apply
#    ⚠️  Never commit secrets.yaml with real values
kubectl apply -f k8s/secrets.yaml

# 4. Apply ConfigMap
kubectl apply -f k8s/configmap.yaml

# 5. Deploy webhook receiver
kubectl apply -f k8s/webhook-deployment.yaml
kubectl apply -f k8s/webhook-service.yaml

# 6. (Optional) Expose via Ingress
#    Edit k8s/webhook-ingress.yaml and uncomment the YAML, then:
kubectl apply -f k8s/webhook-ingress.yaml
```

Verify the deployment:
```bash
kubectl -n openhands rollout status deployment/openhands-webhook
kubectl -n openhands get pods
curl http://<SERVICE_IP>/healthz   # should return {"status":"ok"}
```

---

## Configuring the GitLab Webhook

1. Navigate to **Project → Settings → Webhooks** (or Group → Settings → Webhooks).
2. Click **Add new webhook**.
3. Fill in:

| Field | Value |
|-------|-------|
| URL | `https://openhands-webhook.example.com/webhook` |
| Secret token | The value you set as `WEBHOOK_SECRET` in `k8s/secrets.yaml` |
| Trigger | ☑️ **Comments** (Note events) |

4. Uncheck all other triggers.
5. Click **Add webhook** and then **Test → Comment events** to verify connectivity.

> **Note:** GitLab sends the event header `X-Gitlab-Event: Note Hook`.  The receiver rejects all other event types.

---

## Supported Commands

Comment on any GitLab Issue or Merge Request with one of:

### `@openhands review`
*Works on Merge Requests only.*

```
@openhands review
```

The runner fetches the MR diff and posts a structured review with sections:
- Summary
- Major Issues
- Minor Issues
- Suggested Tests
- Security Notes

### `@openhands fix`
*Works on Issues and Merge Requests.*

```
@openhands fix
```

The runner:
1. Reads the issue/MR description.
2. Clones the repository.
3. Creates a branch:
   - Issues → `ai/issue-<iid>-<short-slug>`
   - MRs    → `ai/mr-<iid>-fix`
4. Generates code changes via the LLM.
5. Runs the test suite (pytest / npm test / go test).
6. If tests pass: commits, pushes, and opens a new MR.
7. Comments a link back to the original issue/MR.

---

## Environment Variables Reference

### Webhook Receiver (ConfigMap + Secret)

| Variable | Source | Description |
|----------|--------|-------------|
| `WEBHOOK_SECRET` | Secret | Shared secret validated against `X-Gitlab-Token` header |
| `GITLAB_BASE_URL` | ConfigMap | GitLab instance URL, e.g. `https://gitlab.example.com` |
| `GITLAB_TOKEN` | Secret | GitLab PAT with api + read/write_repository scopes |
| `K8S_NAMESPACE` | ConfigMap | Namespace for runner Jobs (default: current pod namespace) |
| `JOB_IMAGE` | ConfigMap | Container image for runner Jobs |
| `LLM_BASE_URL` | ConfigMap | vLLM endpoint including `/v1`, e.g. `http://vllm:8000/v1` |
| `LLM_MODEL` | ConfigMap | Model name registered in vLLM |
| `LLM_API_KEY` | Secret | API key for vLLM (any string if auth disabled) |
| `ALLOWED_USERS` | ConfigMap | Comma-separated GitLab usernames; empty = allow all |
| `JOB_TTL_SECONDS` | ConfigMap | Job TTL after completion (default: `1800`) |
| `JOB_CPU_LIMIT` | ConfigMap | CPU limit for runner Jobs (default: `4`) |
| `JOB_MEM_LIMIT` | ConfigMap | Memory limit for runner Jobs (default: `8Gi`) |

### Runner Job (injected by webhook receiver)

| Variable | Description |
|----------|-------------|
| `TASK_KIND` | `review`, `fix_issue`, or `fix_mr` |
| `PROJECT_ID` | GitLab project ID |
| `MR_IID` | MR internal ID (set for review and fix_mr) |
| `ISSUE_IID` | Issue internal ID (set for fix_issue) |
| `NOTE_ID` | Triggering comment note ID |
| `KIND` | `issue` or `mr` |
| `GITLAB_BASE_URL` | Passed through from receiver |
| `GITLAB_TOKEN` | Passed through from receiver |
| `LLM_BASE_URL` | Passed through from receiver |
| `LLM_MODEL` | Passed through from receiver |
| `LLM_API_KEY` | Passed through from receiver |

---

## Troubleshooting

### Webhook receiver returns 401
- Check that the `WEBHOOK_SECRET` in the Secret matches the token configured in the GitLab webhook settings.
- Inspect the `X-Gitlab-Token` header in the GitLab webhook test page.

### Webhook receiver returns 422
- The payload could not be parsed. Check that the **Trigger** is set to **Comments** only, not Merge Requests or Issues separately.

### No 👀 reaction appears
- Check that `GITLAB_TOKEN` has the `api` scope.
- Check receiver logs: `kubectl -n openhands logs deploy/openhands-webhook`
- Verify the bot account has at least **Reporter** access to the project.

### Job is created but no 🚀 reaction
- Inspect receiver logs for `Could not add 'rocket' reaction`.
- The bot account may lack permission to add reactions (needs **Reporter** or higher).

### Runner Job fails / no MR created
- Fetch Job logs: `kubectl -n openhands logs job/<job-name>`
- Common causes:
  - `GITLAB_TOKEN` lacks `write_repository` scope → push fails.
  - LLM returned no FILE blocks → check `LLM_BASE_URL` and `LLM_MODEL`.
  - Test suite fails → fix the tests or the generated code.

### Duplicate Jobs
- The receiver derives a deterministic Job name from `project_id + note_id + task_kind`.
- If the same comment triggers the webhook twice, the second request will detect the existing Job and skip creation, but still add the 🚀 reaction.

### Checking runner Job status
```bash
kubectl -n openhands get jobs
kubectl -n openhands describe job <job-name>
kubectl -n openhands logs job/<job-name>
```
