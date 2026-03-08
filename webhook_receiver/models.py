"""Pydantic models for GitLab Note Hook webhook payload parsing."""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class GitLabUser(BaseModel):
    id: int
    name: str
    username: str


class GitLabProject(BaseModel):
    id: int
    name: str
    path_with_namespace: str
    web_url: str
    git_http_url: Optional[str] = None
    default_branch: Optional[str] = "main"
    homepage: Optional[str] = None
    url: Optional[str] = None
    ssh_url: Optional[str] = None
    http_url: Optional[str] = None


class GitLabNote(BaseModel):
    id: int
    note: str
    noteable_type: str  # "Issue", "MergeRequest", etc.
    author_id: int
    created_at: str
    updated_at: str
    project_id: Optional[int] = None
    attachment: Optional[str] = None
    line_code: Optional[str] = None
    commit_id: Optional[str] = None
    noteable_id: Optional[int] = None
    system: bool = False
    st_diff: Optional[dict] = None
    url: Optional[str] = None


class GitLabIssue(BaseModel):
    id: int
    title: str
    iid: int
    state: str
    description: Optional[str] = None
    url: Optional[str] = None


class GitLabMergeRequest(BaseModel):
    id: int
    title: str
    iid: int
    state: str
    source_branch: str
    target_branch: str
    description: Optional[str] = None
    url: Optional[str] = None
    last_commit: Optional[dict] = None


class NoteHookPayload(BaseModel):
    """GitLab Note Hook payload."""

    object_kind: str = Field(..., description="Should be 'note'")
    event_type: Optional[str] = None
    user: GitLabUser
    project_id: int
    project: GitLabProject
    repository: Optional[dict] = None
    object_attributes: GitLabNote
    issue: Optional[GitLabIssue] = None
    merge_request: Optional[GitLabMergeRequest] = None
