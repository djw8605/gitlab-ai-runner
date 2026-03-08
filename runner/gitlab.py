"""GitLab REST API client for the runner job.

Handles:
- Fetching MR metadata and diffs
- Fetching Issue details
- Posting notes/comments
- Creating Merge Requests
- Adding emoji reactions
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class GitLabError(Exception):
    """Raised when a GitLab API call fails unexpectedly."""


class GitLabClient:
    """Thin wrapper around the GitLab REST API used inside the runner Job."""

    def __init__(self, base_url: str, token: str, timeout: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
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

    # ------------------------------------------------------------------
    # Host extraction (for clone URL construction)
    # ------------------------------------------------------------------

    @property
    def host(self) -> str:
        """Return just the scheme + host of the base URL (no trailing slash)."""
        from urllib.parse import urlparse

        parsed = urlparse(self._base_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def token(self) -> str:
        return self._token

    # ------------------------------------------------------------------
    # MR helpers
    # ------------------------------------------------------------------

    def get_mr(self, project_id: int, mr_iid: int) -> dict:
        """Return MR metadata dict."""
        return self._get(f"/projects/{project_id}/merge_requests/{mr_iid}")

    def get_mr_changes(self, project_id: int, mr_iid: int) -> dict:
        """Return MR changes/diff dict."""
        return self._get(f"/projects/{project_id}/merge_requests/{mr_iid}/changes")

    def get_mr_notes(self, project_id: int, mr_iid: int) -> list[dict]:
        """Return MR notes/comments sorted oldest to newest (first page)."""
        data = self._get(
            f"/projects/{project_id}/merge_requests/{mr_iid}/notes",
            params={"order_by": "created_at", "sort": "asc", "per_page": 100},
        )
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # Issue helpers
    # ------------------------------------------------------------------

    def get_issue(self, project_id: int, issue_iid: int) -> dict:
        """Return issue metadata dict."""
        return self._get(f"/projects/{project_id}/issues/{issue_iid}")

    def get_issue_notes(self, project_id: int, issue_iid: int) -> list[dict]:
        """Return issue notes/comments sorted oldest to newest (first page)."""
        data = self._get(
            f"/projects/{project_id}/issues/{issue_iid}/notes",
            params={"order_by": "created_at", "sort": "asc", "per_page": 100},
        )
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # Project helpers
    # ------------------------------------------------------------------

    def get_project(self, project_id: int) -> dict:
        """Return project metadata dict."""
        return self._get(f"/projects/{project_id}")

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

    def post_note(self, project_id: int, kind: str, iid: int, body: str) -> dict:
        """Post a note to either an issue or MR."""
        if kind == "issue":
            return self.post_issue_note(project_id, iid, body)
        elif kind == "mr":
            return self.post_mr_note(project_id, iid, body)
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
        """Create a new MR and return the response dict."""
        return self._post(
            f"/projects/{project_id}/merge_requests",
            {
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
            },
        )

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    def add_note_reaction(
        self,
        project_id: int,
        kind: str,
        iid: int,
        note_id: int,
        emoji_name: str,
    ) -> None:
        """Add an emoji reaction to a note. Idempotent (409 → success)."""
        if kind == "mr":
            noteable = "merge_requests"
        elif kind == "issue":
            noteable = "issues"
        else:
            raise ValueError(f"Unknown kind: {kind!r}")

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
                logger.debug("Reaction %r already exists (409 – OK)", emoji_name)
                return
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("Failed to add reaction %r: %s", emoji_name, exc.response.text)
            raise GitLabError(f"Could not add reaction {emoji_name!r}: {exc}") from exc
        except httpx.RequestError as exc:
            logger.error("Failed to add reaction %r: %s", emoji_name, exc)
            raise GitLabError(f"Could not add reaction {emoji_name!r}: {exc}") from exc
