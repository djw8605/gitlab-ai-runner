"""GitLab REST API client for the webhook receiver.

Handles:
- Adding emoji reactions to notes (👀 and 🚀)
- Posting notes/comments to Issues and MRs
- Creating Merge Requests
"""

from __future__ import annotations

import logging
from typing import Optional

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
        resp = httpx.post(
            url,
            headers=self._headers,
            json=json,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, params: Optional[dict] = None) -> dict | list:
        url = self._api(path)
        resp = httpx.get(
            url,
            headers=self._headers,
            params=params,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

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
