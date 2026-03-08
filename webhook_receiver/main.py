"""FastAPI webhook receiver for GitLab @openhands mention automation."""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .gitlab import GitLabClient, GitLabError
from .k8s import create_job, job_exists, make_job_name
from .models import NoteHookPayload

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="GitLab OpenHands Webhook Receiver", version="1.0.0")

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name!r} is not set")
    return value


def _get_gitlab_client() -> GitLabClient:
    return GitLabClient(
        base_url=_require_env("GITLAB_BASE_URL"),
        token=_require_env("GITLAB_TOKEN"),
    )


def _get_namespace() -> str:
    ns = os.environ.get("K8S_NAMESPACE", "").strip()
    if ns:
        return ns
    # Try to read from the pod's namespace file (in-cluster)
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return "default"


def _get_allowed_users() -> Optional[set[str]]:
    raw = os.environ.get("ALLOWED_USERS", "").strip()
    if not raw:
        return None
    return {u.strip() for u in raw.split(",") if u.strip()}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/healthz", status_code=status.HTTP_200_OK)
async def healthz() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------


@app.post("/webhook", status_code=status.HTTP_200_OK)
async def webhook(
    request: Request,
    x_gitlab_token: Optional[str] = Header(None, alias="X-Gitlab-Token"),
    x_gitlab_event: Optional[str] = Header(None, alias="X-Gitlab-Event"),
) -> JSONResponse:
    """Handle incoming GitLab Note Hook events."""

    # --- Validate shared secret -------------------------------------------
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "").strip()
    if webhook_secret and x_gitlab_token != webhook_secret:
        logger.warning("Invalid or missing X-Gitlab-Token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    # --- Accept only Note Hook events --------------------------------------
    if x_gitlab_event != "Note Hook":
        logger.debug("Ignoring event type: %s", x_gitlab_event)
        return JSONResponse({"status": "ignored", "reason": "not a Note Hook"})

    # --- Parse payload -----------------------------------------------------
    try:
        raw = await request.json()
        payload = NoteHookPayload(**raw)
    except Exception as exc:
        logger.error("Failed to parse payload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid payload: {exc}",
        )

    note_text: str = payload.object_attributes.note.strip()
    author_username: str = payload.user.username
    note_id: int = payload.object_attributes.id

    logger.info(
        "Received note from %s on project %d: %r",
        author_username,
        payload.project_id,
        note_text[:120],
    )

    # --- Ignore notes that don't start with @openhands --------------------
    if not note_text.lower().startswith("@openhands"):
        return JSONResponse({"status": "ignored", "reason": "not an @openhands mention"})

    # --- Allowlist check --------------------------------------------------
    allowed = _get_allowed_users()
    if allowed is not None and author_username not in allowed:
        logger.info("User %r not in ALLOWED_USERS – ignoring", author_username)
        return JSONResponse({"status": "ignored", "reason": "user not in allowlist"})

    # --- Determine task kind and context ----------------------------------
    note_lower = note_text.lower()
    noteable_type = payload.object_attributes.noteable_type  # "Issue" or "MergeRequest"

    task_kind: Optional[str] = None
    mr_iid: Optional[int] = None
    issue_iid: Optional[int] = None
    kind: str  # "issue" or "mr" – used for GitLab reactions/notes API

    if note_lower.startswith("@openhands review"):
        if noteable_type != "MergeRequest" or payload.merge_request is None:
            return JSONResponse(
                {"status": "ignored", "reason": "'review' only works on Merge Requests"}
            )
        task_kind = "review"
        mr_iid = payload.merge_request.iid
        kind = "mr"

    elif note_lower.startswith("@openhands fix"):
        if noteable_type == "MergeRequest" and payload.merge_request is not None:
            task_kind = "fix_mr"
            mr_iid = payload.merge_request.iid
            kind = "mr"
        elif noteable_type == "Issue" and payload.issue is not None:
            task_kind = "fix_issue"
            issue_iid = payload.issue.iid
            kind = "issue"
        else:
            return JSONResponse(
                {"status": "ignored", "reason": "could not determine target for 'fix'"}
            )

    else:
        return JSONResponse(
            {"status": "ignored", "reason": "unrecognized @openhands command"}
        )

    iid = mr_iid if mr_iid is not None else issue_iid
    project_id = payload.project_id

    logger.info(
        "Handling task_kind=%s project=%d %s=%s note_id=%d",
        task_kind,
        project_id,
        kind,
        iid,
        note_id,
    )

    # --- Set up GitLab client ---------------------------------------------
    try:
        gl = _get_gitlab_client()
    except RuntimeError as exc:
        logger.error("GitLab client config error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server misconfiguration",
        )

    # --- Add 👀 reaction immediately (before job creation) ----------------
    try:
        gl.add_note_reaction(project_id, kind, iid, note_id, "eyes")
    except GitLabError as exc:
        logger.warning("Could not add 'eyes' reaction: %s", exc)
        # Non-fatal – continue with job creation

    # --- Idempotency check: avoid duplicate jobs --------------------------
    job_name = make_job_name(project_id, note_id, task_kind)
    namespace = _get_namespace()

    already_exists = False
    try:
        already_exists = job_exists(namespace, job_name)
    except Exception as exc:
        logger.error("Error checking job existence: %s", exc)

    if already_exists:
        logger.info("Job %s already exists – skipping creation", job_name)
        try:
            gl.add_note_reaction(project_id, kind, iid, note_id, "rocket")
        except GitLabError as exc:
            logger.warning("Could not add 'rocket' reaction (existing job): %s", exc)
        return JSONResponse({"status": "already_exists", "job_name": job_name})

    # --- Build env vars for the runner Job --------------------------------
    env_vars: dict[str, str] = {
        "TASK_KIND": task_kind,
        "PROJECT_ID": str(project_id),
        "NOTE_ID": str(note_id),
        "KIND": kind,
        "GITLAB_BASE_URL": os.environ.get("GITLAB_BASE_URL", ""),
        "GITLAB_TOKEN": os.environ.get("GITLAB_TOKEN", ""),
        "LLM_BASE_URL": os.environ.get("LLM_BASE_URL", ""),
        "LLM_MODEL": os.environ.get("LLM_MODEL", ""),
        "LLM_API_KEY": os.environ.get("LLM_API_KEY", ""),
    }
    if mr_iid is not None:
        env_vars["MR_IID"] = str(mr_iid)
    if issue_iid is not None:
        env_vars["ISSUE_IID"] = str(issue_iid)

    ttl = int(os.environ.get("JOB_TTL_SECONDS", "1800"))
    image = os.environ.get("JOB_IMAGE", "")
    if not image:
        logger.error("JOB_IMAGE is not set")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JOB_IMAGE not configured",
        )

    # --- Create the Kubernetes Job ----------------------------------------
    try:
        create_job(
            namespace=namespace,
            job_name=job_name,
            image=image,
            env_vars=env_vars,
            ttl_seconds=ttl,
        )
    except Exception as exc:
        logger.error("Failed to create Job %s: %s", job_name, exc)
        # Post a failure comment; do NOT add 🚀
        try:
            gl.post_note(
                project_id,
                kind,
                iid,
                f"⚠️ **OpenHands**: Failed to start runner job.\n\n```\n{exc}\n```",
            )
        except GitLabError as gl_exc:
            logger.warning("Could not post failure note: %s", gl_exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Job creation failed: {exc}",
        )

    # --- Add 🚀 reaction after successful job creation --------------------
    try:
        gl.add_note_reaction(project_id, kind, iid, note_id, "rocket")
    except GitLabError as exc:
        logger.warning("Could not add 'rocket' reaction: %s", exc)

    return JSONResponse(
        {
            "status": "job_created",
            "job_name": job_name,
            "task_kind": task_kind,
            "namespace": namespace,
        }
    )
