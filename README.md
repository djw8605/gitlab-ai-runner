# gitlab-ai-runner

A self-hosted GitLab **@crush** mention automation that launches Kubernetes Jobs to run an OpenCode-based coding agent against an OpenAI-compatible endpoint (for example, vLLM).

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
                         │          → opencode (batch) → posts MR note
                         └─ fix:    clones repo, gathers issue/MR context + prompt tail
                                    → opencode (batch + tools) → commits → pushes
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
│   ├── llm.py           # Legacy LLM wrapper (not used by the opencode runner)
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
│   ├── webhook-ingress.yaml  # optional – apply separately
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
kubectl create namespace unl-weitzel

# 2. Set non-secret runtime config
$EDITOR k8s/config.env

# 3. Create secrets input (do not commit k8s/secrets.env)
cp k8s/secrets.env.example k8s/secrets.env
$EDITOR k8s/secrets.env

# 4. Deploy all required resources (RBAC + ConfigMap + Secret + Deployment + Service)
kubectl apply -k k8s

# 5. (Optional) Expose via Ingress
#    Adjust the host in k8s/webhook-ingress.yaml if needed, then:
kubectl apply -f k8s/webhook-ingress.yaml
```

Verify the deployment:
```bash
kubectl -n unl-weitzel rollout status deployment/crush-webhook
kubectl -n unl-weitzel get pods
curl http://<SERVICE_IP>/healthz   # should return {"status":"ok"}
```

---

## Configuring the GitLab Webhook

1. Navigate to **Project → Settings → Webhooks** (or Group → Settings → Webhooks).
2. Click **Add new webhook**.
3. Fill in:

| Field | Value |
|-------|-------|
| URL | `https://crush-webhook-unl-weitzel.nrp-nautilus.io/webhook` |
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

Anything after `@crush` is forwarded to the runner as additional prompt text.

### `@crush fix`
*Works on Issues and Merge Requests.*

```
@crush fix
@crush fix prioritize minimal patch, and add regression test
```

For **Issue** fixes, the webhook first:
1. Creates/reuses branch `ai/issue-<iid>-<short-slug>`.
2. Creates/reuses a merge request from that branch.
3. Replies on the issue with the MR link.
4. Starts the runner against that MR branch.

Then the runner:
1. Reads the issue/MR context and prompt text.
2. Clones the repository and checks out the target branch.
3. Runs `opencode` in batch mode with OpenAI-compatible custom provider config.
4. Sends task context directly (no system prompt scaffolding).
5. Avoids system package installs unless strictly required; prefers repo-local/file-first changes.
6. Runs the test suite (pytest / npm test / go test) when quick and available.
7. If tests pass: commits and pushes updates to the existing branch.
8. Posts update notes back to GitLab.

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
| `OPENCODE_BASE_URL` | ConfigMap | OpenAI-compatible endpoint including `/v1`, e.g. `http://vllm:8000/v1` |
| `OPENCODE_MODEL` | ConfigMap | Model name exposed by your provider |
| `OPENCODE_API_KEY` | Secret | API key for the provider (any string if auth is disabled) |
| `OPENCODE_TIMEOUT_SECONDS` | ConfigMap | Timeout for each opencode invocation (default: `1800`) |
| `OPENCODE_MAX_CONTEXT_TOKENS` | ConfigMap | Context token limit written into opencode provider config (default: `128000`) |
| `OPENCODE_MAX_OUTPUT_TOKENS` | ConfigMap | Max output tokens in opencode config (default: `4096`) |
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
| `OPENCODE_BASE_URL` | Passed through from receiver |
| `OPENCODE_MODEL` | Passed through from receiver |
| `OPENCODE_API_KEY` | Passed through from receiver |
| `OPENCODE_TIMEOUT_SECONDS` | Passed through from receiver |
| `OPENCODE_MAX_CONTEXT_TOKENS` | Passed through from receiver |
| `OPENCODE_MAX_OUTPUT_TOKENS` | Passed through from receiver |
| `OPENCODE_USER_PROMPT` | Entire text after `@crush` from the triggering comment |
| `PRECREATED_MR_IID` | For issue fixes: MR IID prepared by webhook |
| `PRECREATED_MR_URL` | For issue fixes: MR URL prepared by webhook |
| `PRECREATED_MR_BRANCH` | For issue fixes: branch prepared by webhook |
| `PRECREATED_MR_TARGET_BRANCH` | For issue fixes: MR target branch prepared by webhook |

---

## Troubleshooting

### Webhook receiver returns 401
- Check that the `WEBHOOK_SECRET` in the Secret matches the token configured in the GitLab webhook settings.
- Inspect the `X-Gitlab-Token` header in the GitLab webhook test page.

### Webhook receiver returns 422
- The payload could not be parsed. Check that the **Trigger** is set to **Comments** only, not Merge Requests or Issues separately.

### Webhook receiver returns 500 with `No address associated with hostname`
- `GITLAB_BASE_URL` is not resolvable from the webhook pod.
- Verify DNS from inside the namespace (example):
  - `kubectl -n unl-weitzel exec deploy/crush-webhook -- getent hosts gitlab.nrp-nautilus.io`
- If GitLab is in-cluster, prefer an internal service URL for `GITLAB_BASE_URL` (for example, `http://<gitlab-service>.<namespace>.svc.cluster.local`).

### No 👀 reaction appears
- Check that `GITLAB_TOKEN` has the `api` scope.
- Check receiver logs: `kubectl -n unl-weitzel logs deploy/crush-webhook`
- Verify the bot account has at least **Reporter** access to the project.

### Job is created but no 🚀 reaction
- Inspect receiver logs for `Could not add 'rocket' reaction`.
- The bot account may lack permission to add reactions (needs **Reporter** or higher).

### Runner Job fails / no MR created
- Fetch Job logs: `kubectl -n unl-weitzel logs job/<job-name>`
- Common causes:
  - `GITLAB_TOKEN` lacks `write_repository` scope → push fails.
  - `opencode` provider configuration invalid or unreachable → check `OPENCODE_BASE_URL`, `OPENCODE_MODEL`, and `OPENCODE_API_KEY`.
  - Provider rejects output token limits → set `OPENCODE_MAX_OUTPUT_TOKENS` to a positive integer.
  - OpenCode returned without edits → runner fails fast with `No filesystem changes detected.`
  - Test suite fails → fix the tests or the generated code.

### Duplicate Jobs
- The receiver derives a deterministic Job name from `project_id + note_id + task_kind`.
- If the same comment triggers the webhook twice, the second request will detect the existing Job and skip creation, but still add the 🚀 reaction.

### Checking runner Job status
```bash
kubectl -n unl-weitzel get jobs
kubectl -n unl-weitzel describe job <job-name>
kubectl -n unl-weitzel logs job/<job-name>
```

Runner logs include streamed `opencode` stdout/stderr lines (prefixed as `opencode stdout | ...` / `opencode stderr | ...`) and post-run diagnostics.
Runner logs also include fail-fast diagnostics after each opencode run:
- OpenCode command + exit code
- Tail of stdout/stderr
- `git status --porcelain`
- `git diff --stat` when changes exist
