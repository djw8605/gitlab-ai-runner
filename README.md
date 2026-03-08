# gitlab-ai-runner

A self-hosted GitLab **@crush** mention automation that launches Kubernetes Jobs to run a Crush-based coding agent against an OpenAI-compatible endpoint (for example, vLLM).

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
                         ├─ parses @crush command + user prompt tail
                         ├─ adds 👀 reaction to triggering comment
                         ├─ creates Kubernetes Job (runner)
                         └─ adds 🚀 reaction after successful job creation

                    Kubernetes Job (ephemeral runner)
                         │
                         ├─ review: fetches MR diff + MR comments + prompt tail
                         │          → crush (batch) → posts MR note
                         └─ fix:    clones repo, gathers issue/MR context + prompt tail
                                    → crush (batch + tools) → commits → pushes
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
│   ├── llm.py           # Legacy LLM wrapper (not used by the crush runner)
│   ├── workspace.py     # git clone/branch/commit/push + test runner
│   └── requirements.txt
├── Dockerfile.webhook   # Image for the webhook receiver Deployment
├── Dockerfile.runner    # Image for the ephemeral runner Job
├── k8s/
│   ├── kustomization.yaml
│   ├── config.env
│   ├── secrets.env.example
│   ├── serviceaccount.yaml
│   ├── role.yaml
│   ├── rolebinding.yaml
│   ├── webhook-deployment.yaml
│   ├── webhook-service.yaml
│   ├── webhook-ingress.yaml  # optional – disabled by default
│   ├── configmap.yaml        # legacy static manifest (optional)
│   └── secrets.yaml          # legacy static manifest (optional)
└── README.md
```

---

## Emoji Reaction Behaviour

| Step | Action | Emoji |
|------|--------|-------|
| 1 | Webhook validated; comment starts with `@crush`; user passes allowlist | 👀 `eyes` |
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

The webhook receiver runs under the `crush-webhook` ServiceAccount.  All RBAC is **namespace-scoped** (not cluster-wide).

| Resource | API Group | Verbs | Reason |
|----------|-----------|-------|--------|
| `jobs` | `batch` | `create, get, list, watch, delete` | Create and inspect runner Jobs; delete is optional but enables cleanup |
| `pods` | *(core)* | `get, list, watch` | Optional: allows the receiver to link to pod logs when diagnosing failures |

These RBAC resources are included in `k8s/kustomization.yaml` and applied with `kubectl apply -k k8s`.

---

## Building and Pushing Images

```bash
# Set your container registry
export REGISTRY=registry.example.com/myorg

# Build and push the webhook receiver
docker build -f Dockerfile.webhook -t $REGISTRY/crush-webhook:latest .
docker push $REGISTRY/crush-webhook:latest

# Build and push the runner Job image
docker build -f Dockerfile.runner -t $REGISTRY/crush-runner:latest .
docker push $REGISTRY/crush-runner:latest
```

Then update `k8s/config.env` with your actual `WEBHOOK_IMAGE` and `JOB_IMAGE` values.

---

## Deploying to Kubernetes

```bash
# 1. Create the namespace (if it doesn't exist)
kubectl create namespace crush

# 2. Set non-secret runtime config
$EDITOR k8s/config.env

# 3. Create secrets input (do not commit k8s/secrets.env)
cp k8s/secrets.env.example k8s/secrets.env
$EDITOR k8s/secrets.env

# 4. Deploy all required resources (RBAC + ConfigMap + Secret + Deployment + Service)
kubectl apply -k k8s

# 5. (Optional) Expose via Ingress
#    Edit k8s/webhook-ingress.yaml and uncomment the YAML, then:
kubectl apply -f k8s/webhook-ingress.yaml
```

Verify the deployment:
```bash
kubectl -n crush rollout status deployment/crush-webhook
kubectl -n crush get pods
curl http://<SERVICE_IP>/healthz   # should return {"status":"ok"}
```

---

## Configuring the GitLab Webhook

1. Navigate to **Project → Settings → Webhooks** (or Group → Settings → Webhooks).
2. Click **Add new webhook**.
3. Fill in:

| Field | Value |
|-------|-------|
| URL | `https://crush-webhook.example.com/webhook` |
| Secret token | The value you set as `WEBHOOK_SECRET` in `k8s/secrets.env` |
| Trigger | ☑️ **Comments** (Note events) |

4. Uncheck all other triggers.
5. Click **Add webhook** and then **Test → Comment events** to verify connectivity.

> **Note:** GitLab sends the event header `X-Gitlab-Event: Note Hook`.  The receiver rejects all other event types.

---

## Supported Commands

Comment on any GitLab Issue or Merge Request with one of:

### `@crush review`
*Works on Merge Requests only.*

```
@crush review
@crush review focus on auth edge-cases and suggest tests
```

The runner fetches the MR diff and posts a structured review with sections:
- Summary
- Major Issues
- Minor Issues
- Suggested Tests
- Security Notes

Anything after `@crush` is forwarded to crush as additional prompt text.

### `@crush fix`
*Works on Issues and Merge Requests.*

```
@crush fix
@crush fix prioritize minimal patch, and add regression test
```

The runner:
1. Reads the issue/MR description.
2. Clones the repository.
3. Creates a branch:
   - Issues → `ai/issue-<iid>-<short-slug>`
   - MRs    → `ai/mr-<iid>-fix`
4. Runs `crush` in batch mode (`--yolo`) so it can use tools and edit files without interactive permission prompts.
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
| `WEBHOOK_IMAGE` | ConfigMap | Container image for webhook Deployment |
| `JOB_IMAGE` | ConfigMap | Container image for runner Jobs |
| `CRUSH_BASE_URL` | ConfigMap | Crush model provider endpoint including `/v1`, e.g. `http://vllm:8000/v1` |
| `CRUSH_MODEL` | ConfigMap | Model name exposed by your provider |
| `CRUSH_API_KEY` | Secret | API key for the provider (any string if auth is disabled) |
| `CRUSH_ALLOWED_TOOLS` | ConfigMap | Comma-separated tools auto-allowed in crush config (default: `view,ls,grep,edit,bash`) |
| `CRUSH_TIMEOUT_SECONDS` | ConfigMap | Timeout for each crush invocation (default: `1800`) |
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
| `CRUSH_BASE_URL` | Passed through from receiver |
| `CRUSH_MODEL` | Passed through from receiver |
| `CRUSH_API_KEY` | Passed through from receiver |
| `CRUSH_ALLOWED_TOOLS` | Passed through from receiver |
| `CRUSH_TIMEOUT_SECONDS` | Passed through from receiver |
| `CRUSH_USER_PROMPT` | Entire text after `@crush` from the triggering comment |

---

## Troubleshooting

### Webhook receiver returns 401
- Check that the `WEBHOOK_SECRET` in the Secret matches the token configured in the GitLab webhook settings.
- Inspect the `X-Gitlab-Token` header in the GitLab webhook test page.

### Webhook receiver returns 422
- The payload could not be parsed. Check that the **Trigger** is set to **Comments** only, not Merge Requests or Issues separately.

### No 👀 reaction appears
- Check that `GITLAB_TOKEN` has the `api` scope.
- Check receiver logs: `kubectl -n crush logs deploy/crush-webhook`
- Verify the bot account has at least **Reporter** access to the project.

### Job is created but no 🚀 reaction
- Inspect receiver logs for `Could not add 'rocket' reaction`.
- The bot account may lack permission to add reactions (needs **Reporter** or higher).

### Runner Job fails / no MR created
- Fetch Job logs: `kubectl -n crush logs job/<job-name>`
- Common causes:
  - `GITLAB_TOKEN` lacks `write_repository` scope → push fails.
  - `crush` provider configuration invalid or unreachable → check `CRUSH_BASE_URL`, `CRUSH_MODEL`, and `CRUSH_API_KEY`.
  - Test suite fails → fix the tests or the generated code.

### Duplicate Jobs
- The receiver derives a deterministic Job name from `project_id + note_id + task_kind`.
- If the same comment triggers the webhook twice, the second request will detect the existing Job and skip creation, but still add the 🚀 reaction.

### Checking runner Job status
```bash
kubectl -n crush get jobs
kubectl -n crush describe job <job-name>
kubectl -n crush logs job/<job-name>
```

Runner logs include streamed `crush` stdout/stderr lines (prefixed as `crush stdout | ...` / `crush stderr | ...`), including the final `## Thinking` and `## Files Changed` sections from batch mode output.
