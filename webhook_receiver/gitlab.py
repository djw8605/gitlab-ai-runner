"""GitLab REST API client for the webhook receiver.

Handles:
- Adding emoji reactions to notes (👀 and 🚀)
- Posting notes/comments to Issues and MRs
- Creating Merge Requests
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class GitLabError(Exception):
    """Raised when a GitLab API call fails unexpectedly."""


class GitLabClient:
    """Thin wrapper around the GitLab REST API."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "PRIVATE-TOKEN": token,
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _api(self, path: str) -> str:
        return f"{self._base_url}/api/v4{path}"

    def _post(self, path: str, json: dict) -> dict:
        url = self._api(path)
        try:
            resp = httpx.post(
                url,
                headers=self._headers,
                json=json,
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("GitLab POST %s failed: %s", url, exc.response.text)
            raise GitLabError(f"GitLab POST failed: {url}") from exc
        except httpx.RequestError as exc:
            logger.error("GitLab POST %s failed: %s", url, exc)
            raise GitLabError(f"GitLab POST failed: {url}") from exc
        return resp.json()

    def _get(self, path: str, params: Optional[dict] = None) -> dict | list:
        url = self._api(path)
        try:
            resp = httpx.get(
                url,
                headers=self._headers,
                params=params,
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("GitLab GET %s failed: %s", url, exc.response.text)
            raise GitLabError(f"GitLab GET failed: {url}") from exc
        except httpx.RequestError as exc:
            logger.error("GitLab GET %s failed: %s", url, exc)
            raise GitLabError(f"GitLab GET failed: {url}") from exc
        return resp.json()

    def _get_optional(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """GET helper that returns None for 404 instead of raising."""
        url = self._api(path)
        try:
            resp = httpx.get(
                url,
                headers=self._headers,
                params=params,
                timeout=self._timeout,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("GitLab GET %s failed: %s", url, exc.response.text)
            raise GitLabError(f"GitLab GET failed: {url}") from exc
        except httpx.RequestError as exc:
            logger.error("GitLab GET %s failed: %s", url, exc)
            raise GitLabError(f"GitLab GET failed: {url}") from exc

    # ------------------------------------------------------------------
    # Reactions (Emoji awards)
    # ------------------------------------------------------------------

    def add_note_reaction(
        self,
        project_id: int,
        kind: str,  # "issue" or "mr"
        iid: int,
        note_id: int,
        emoji_name: str,
    ) -> None:
        """Add an emoji reaction to a specific note.

        Idempotent: 409 (already exists) is treated as success.

        Args:
            project_id: GitLab project ID.
            kind: "issue" or "mr".
            iid: Issue or MR internal ID.
            note_id: The note/comment ID.
            emoji_name: GitLab emoji name, e.g. "eyes" or "rocket".
        """
        if kind == "mr":
            noteable = "merge_requests"
        elif kind == "issue":
            noteable = "issues"
        else:
            raise ValueError(f"Unknown kind: {kind!r}; expected 'issue' or 'mr'")

        path = f"/projects/{project_id}/{noteable}/{iid}/notes/{note_id}/award_emoji"
        url = self._api(path)

        try:
            resp = httpx.post(
                url,
                headers=self._headers,
                json={"name": emoji_name},
                timeout=self._timeout,
            )
            if resp.status_code == 409:
                logger.debug(
                    "Reaction %r already exists on note %d (409 – OK)", emoji_name, note_id
                )
                return
            resp.raise_for_status()
            logger.info(
                "Added reaction %r to %s note %d on %s#%d",
                emoji_name,
                kind,
                note_id,
                project_id,
                iid,
            )
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Failed to add reaction %r to note %d: %s",
                emoji_name,
                note_id,
                exc.response.text,
            )
            raise GitLabError(f"Could not add reaction {emoji_name!r}: {exc}") from exc
        except httpx.RequestError as exc:
            logger.error(
                "Failed to add reaction %r to note %d: %s",
                emoji_name,
                note_id,
                exc,
            )
            raise GitLabError(f"Could not add reaction {emoji_name!r}: {exc}") from exc

    # ------------------------------------------------------------------
    # Notes / Comments
    # ------------------------------------------------------------------

    def post_issue_note(self, project_id: int, issue_iid: int, body: str) -> dict:
        """Post a comment to a GitLab Issue."""
        return self._post(
            f"/projects/{project_id}/issues/{issue_iid}/notes",
            {"body": body},
        )

    def post_mr_note(self, project_id: int, mr_iid: int, body: str) -> dict:
        """Post a comment to a GitLab Merge Request."""
        return self._post(
            f"/projects/{project_id}/merge_requests/{mr_iid}/notes",
            {"body": body},
        )

    def post_note(
        self,
        project_id: int,
        kind: str,
        iid: int,
        body: str,
    ) -> dict:
        """Post a note to either an issue or MR."""
        if kind == "issue":
            return self.post_issue_note(project_id, iid, body)
        elif kind == "mr":
            return self.post_mr_note(project_id, iid, body)
        else:
            raise ValueError(f"Unknown kind: {kind!r}")

    # ------------------------------------------------------------------
    # Merge Requests
    # ------------------------------------------------------------------

    def create_merge_request(
        self,
        project_id: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
    ) -> dict:
        """Create a new Merge Request and return the response dict."""
        return self._post(
            f"/projects/{project_id}/merge_requests",
            {
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
            },
        )

    def list_open_merge_requests_by_source_branch(
        self, project_id: int, source_branch: str
    ) -> list[dict]:
        data = self._get(
            f"/projects/{project_id}/merge_requests",
            params={
                "state": "opened",
                "source_branch": source_branch,
                "order_by": "updated_at",
                "sort": "desc",
                "per_page": 20,
            },
        )
        return data if isinstance(data, list) else []

    def ensure_merge_request(
        self,
        project_id: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
    ) -> dict:
        """Create an MR, or return an existing open MR for the same source branch."""
        existing = self.list_open_merge_requests_by_source_branch(
            project_id, source_branch
        )
        if existing:
            return existing[0]
        return self.create_merge_request(
            project_id=project_id,
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            description=description,
        )

    # ------------------------------------------------------------------
    # Branch helpers
    # ------------------------------------------------------------------

    def get_branch(self, project_id: int, branch: str) -> Optional[dict]:
        enc_branch = quote(branch, safe="")
        return self._get_optional(f"/projects/{project_id}/repository/branches/{enc_branch}")

    def create_branch(self, project_id: int, branch: str, ref: str) -> dict:
        """Create a branch from ref. Returns branch data."""
        url = self._api(f"/projects/{project_id}/repository/branches")
        try:
            resp = httpx.post(
                url,
                headers=self._headers,
                params={"branch": branch, "ref": ref},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("GitLab POST %s failed: %s", url, exc.response.text)
            raise GitLabError(f"GitLab POST failed: {url}") from exc
        except httpx.RequestError as exc:
            logger.error("GitLab POST %s failed: %s", url, exc)
            raise GitLabError(f"GitLab POST failed: {url}") from exc

    def ensure_branch(self, project_id: int, branch: str, ref: str) -> dict:
        """Ensure branch exists by creating it if missing."""
        existing = self.get_branch(project_id, branch)
        if existing is not None:
            return existing
        return self.create_branch(project_id, branch, ref)
