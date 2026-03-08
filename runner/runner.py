"""Runner job entrypoint.

This script is executed inside the Kubernetes Job container.
It reads environment variables, performs the requested task (review or fix),
and posts results back to GitLab.
"""

from __future__ import annotations

import logging
import os
import sys
import textwrap
from pathlib import Path
from typing import Optional

from gitlab import GitLabClient, GitLabError
from llm import LLMClient, LLMError
from workspace import Workspace

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
)
logger = logging.getLogger("runner")

# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        logger.error("Required env var %r is not set", name)
        sys.exit(1)
    return val


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ---------------------------------------------------------------------------
# Review task
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM = textwrap.dedent(
    """\
    You are an expert code reviewer. You will be given a GitLab Merge Request
    title, description, and unified diff. Produce a structured review with the
    following sections:

    ## Summary
    A concise overview of what the MR does.

    ## Major Issues
    Any blocking problems: bugs, security flaws, incorrect logic.

    ## Minor Issues
    Style, naming, small improvements.

    ## Suggested Tests
    Specific test cases that are missing or should be added.

    ## Security Notes
    Any security concerns, even minor ones.

    Be specific and cite file names and line numbers where relevant.
    If a section has no items write "None identified."
    """
)


def _format_diff(changes: dict) -> str:
    """Extract and truncate the diff text from MR changes."""
    diff_parts: list[str] = []
    for change in changes.get("changes", []):
        path = change.get("new_path") or change.get("old_path", "?")
        diff = change.get("diff", "")
        diff_parts.append(f"--- {path} ---\n{diff}")
    full = "\n".join(diff_parts)
    from workspace import MAX_DIFF_CHARS

    if len(full) > MAX_DIFF_CHARS:
        full = full[:MAX_DIFF_CHARS] + "\n… [diff truncated] …"
    return full


def run_review(
    gl: GitLabClient,
    llm: LLMClient,
    project_id: int,
    mr_iid: int,
    kind: str,
    note_id: int,
) -> None:
    """Fetch MR diff, produce a review via LLM, post as a note."""
    logger.info("Starting REVIEW task for MR !%d", mr_iid)

    mr = gl.get_mr(project_id, mr_iid)
    changes = gl.get_mr_changes(project_id, mr_iid)

    title = mr.get("title", "")
    description = mr.get("description", "")
    diff_text = _format_diff(changes)

    user_prompt = (
        f"MR Title: {title}\n\n"
        f"MR Description:\n{description}\n\n"
        f"Diff:\n```diff\n{diff_text}\n```"
    )

    logger.info("Requesting LLM review …")
    try:
        review_text = llm.complete(
            system_prompt=_REVIEW_SYSTEM,
            user_prompt=user_prompt,
            max_tokens=2048,
        )
    except LLMError as exc:
        logger.error("LLM review failed: %s", exc)
        gl.post_mr_note(
            project_id,
            mr_iid,
            f"⚠️ **OpenHands**: LLM review failed.\n\n```\n{exc}\n```",
        )
        sys.exit(1)

    note_body = f"## 🤖 OpenHands Code Review\n\n{review_text}"
    gl.post_mr_note(project_id, mr_iid, note_body)
    logger.info("Posted review to MR !%d", mr_iid)


# ---------------------------------------------------------------------------
# Fix task (shared for issue and MR-fix)
# ---------------------------------------------------------------------------

_FIX_SYSTEM = textwrap.dedent(
    """\
    You are an expert software engineer. You will be given a task description
    (either a bug report or a feature request) and the relevant project context.

    Your job:
    1. Analyse the problem.
    2. Propose a concrete plan (files to modify or create).
    3. Output the FULL content of each file you want to create or modify in the
       following format (repeat for each file):

    FILE: <relative/path/to/file>
    ```
    <complete file content>
    ```
    END_FILE

    Rules:
    - Output ONLY the FILE blocks and a short summary at the end.
    - Do not truncate file content; output the complete file.
    - Keep changes focused and minimal.
    - Do not add unnecessary comments.
    """
)

_FIX_SYSTEM_CONTEXT = textwrap.dedent(
    """\
    You are an expert software engineer. Given a task, produce a brief plan and
    then output complete file contents for each file you want to change or
    create, using the format:

    FILE: <relative/path/to/file>
    ```
    <complete file content>
    ```
    END_FILE

    Keep changes minimal, correct, and focused on the task.
    """
)


def _parse_file_blocks(text: str) -> list[tuple[str, str]]:
    """Parse FILE: / ``` / END_FILE blocks from LLM output.

    Returns a list of (relative_path, content) tuples.
    """
    import re

    pattern = re.compile(
        r"FILE:\s*(.+?)\n```[^\n]*\n(.*?)```\s*END_FILE",
        re.DOTALL,
    )
    return [(m.group(1).strip(), m.group(2)) for m in pattern.finditer(text)]


def run_fix(
    gl: GitLabClient,
    llm: LLMClient,
    ws: Workspace,
    project_id: int,
    kind: str,
    iid: int,
    note_id: int,
    task_kind: str,
) -> None:
    """Fix an issue or MR: generate code changes, push branch, open MR."""
    logger.info("Starting FIX task (%s) for %s #%d", task_kind, kind, iid)

    project = gl.get_project(project_id)
    path_with_namespace: str = project["path_with_namespace"]
    default_branch: str = project.get("default_branch", "main")
    gitlab_base_url = os.environ.get("GITLAB_BASE_URL", "")
    gitlab_token = os.environ.get("GITLAB_TOKEN", "")

    # --- Fetch description ------------------------------------------------
    if task_kind == "fix_issue":
        item = gl.get_issue(project_id, iid)
        item_title: str = item.get("title", f"Issue #{iid}")
        item_description: str = item.get("description", "")
        base_branch = default_branch
        new_branch = ws.issue_branch(iid, item_title)
        back_ref = f"issue #{iid}"
        mr_title = f"fix: resolve issue #{iid} – {item_title}"
        mr_description = (
            f"Closes #{iid}\n\n"
            f"This MR was automatically generated by OpenHands in response to "
            f"[issue #{iid}]({item.get('web_url', '')})."
        )
    else:  # fix_mr
        item = gl.get_mr(project_id, iid)
        item_title = item.get("title", f"MR !{iid}")
        item_description = item.get("description", "")
        base_branch = item.get("target_branch", default_branch)
        new_branch = ws.mr_fix_branch(iid)
        back_ref = f"MR !{iid}"
        mr_title = f"fix: address changes requested in !{iid}"
        mr_description = (
            f"This MR was automatically generated by OpenHands in response to "
            f"[MR !{iid}]({item.get('web_url', '')})."
        )

    # --- Clone repo -------------------------------------------------------
    ws.clone(
        gitlab_base_url=gitlab_base_url,
        path_with_namespace=path_with_namespace,
        token=gitlab_token,
        branch=base_branch,
    )
    ws.create_branch(new_branch)

    # --- Ask LLM for changes ----------------------------------------------
    user_prompt = (
        f"Project: {path_with_namespace}\n"
        f"Task: {item_title}\n\n"
        f"Description:\n{item_description}\n\n"
        f"Produce file changes to resolve this task."
    )

    logger.info("Requesting LLM code generation …")
    try:
        llm_output = llm.complete(
            system_prompt=_FIX_SYSTEM_CONTEXT,
            user_prompt=user_prompt,
            max_tokens=4096,
        )
    except LLMError as exc:
        logger.error("LLM fix generation failed: %s", exc)
        gl.post_note(
            project_id,
            kind,
            iid,
            f"⚠️ **OpenHands**: LLM code generation failed.\n\n```\n{exc}\n```",
        )
        sys.exit(1)

    file_blocks = _parse_file_blocks(llm_output)
    if not file_blocks:
        logger.warning("LLM returned no FILE blocks – nothing to commit")
        gl.post_note(
            project_id,
            kind,
            iid,
            "⚠️ **OpenHands**: The LLM did not produce any file changes. "
            "Please refine the request.",
        )
        sys.exit(1)

    logger.info("LLM produced %d file block(s)", len(file_blocks))
    for rel_path, content in file_blocks:
        ws.write_file(rel_path, content)

    # --- Commit -----------------------------------------------------------
    commit_msg = f"chore: OpenHands automated fix for {back_ref}\n\nTask: {item_title}"
    ws.commit_all(commit_msg)

    if not ws.has_changes() and not _branch_has_commits(ws, base_branch, new_branch):
        gl.post_note(
            project_id,
            kind,
            iid,
            "ℹ️ **OpenHands**: No changes were necessary – the issue may already be resolved.",
        )
        return

    # --- Run tests --------------------------------------------------------
    passed, test_output = ws.run_tests()
    if not passed:
        test_snippet = test_output[-3000:] if len(test_output) > 3000 else test_output
        gl.post_note(
            project_id,
            kind,
            iid,
            f"⚠️ **OpenHands**: Tests failed after applying changes. "
            f"Branch `{new_branch}` was NOT pushed.\n\n```\n{test_snippet}\n```",
        )
        sys.exit(1)

    # --- Push branch ------------------------------------------------------
    ws.push(new_branch)

    # --- Open Merge Request -----------------------------------------------
    try:
        new_mr = gl.create_merge_request(
            project_id=project_id,
            source_branch=new_branch,
            target_branch=base_branch,
            title=mr_title,
            description=mr_description,
        )
        new_mr_url = new_mr.get("web_url", "")
        new_mr_iid = new_mr.get("iid", "?")
    except GitLabError as exc:
        logger.error("Failed to create MR: %s", exc)
        gl.post_note(
            project_id,
            kind,
            iid,
            f"⚠️ **OpenHands**: Branch `{new_branch}` was pushed but MR creation failed.\n"
            f"Please open the MR manually.\n\n```\n{exc}\n```",
        )
        sys.exit(1)

    # --- Comment back to original issue/MR --------------------------------
    gl.post_note(
        project_id,
        kind,
        iid,
        f"🤖 **OpenHands** has created a fix in !{new_mr_iid}: {new_mr_url}\n\n"
        f"Branch: `{new_branch}`",
    )
    logger.info("Fix complete. New MR: %s", new_mr_url)


def _branch_has_commits(ws: Workspace, base_branch: str, new_branch: str) -> bool:
    """Return True if new_branch has commits ahead of base_branch."""
    import subprocess

    result = subprocess.run(
        ["git", "rev-list", "--count", f"{base_branch}..{new_branch}"],
        cwd=ws.repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    task_kind = _require("TASK_KIND")
    project_id = int(_require("PROJECT_ID"))
    kind = _require("KIND")
    note_id = int(_require("NOTE_ID"))

    mr_iid_str = _optional("MR_IID")
    issue_iid_str = _optional("ISSUE_IID")
    mr_iid: Optional[int] = int(mr_iid_str) if mr_iid_str else None
    issue_iid: Optional[int] = int(issue_iid_str) if issue_iid_str else None

    gl = GitLabClient(
        base_url=_require("GITLAB_BASE_URL"),
        token=_require("GITLAB_TOKEN"),
    )
    llm = LLMClient(
        base_url=_require("LLM_BASE_URL"),
        model=_require("LLM_MODEL"),
        api_key=_require("LLM_API_KEY"),
    )

    workspace_root = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
    workspace_root.mkdir(parents=True, exist_ok=True)
    ws = Workspace(workspace_root)

    if task_kind == "review":
        if mr_iid is None:
            logger.error("MR_IID is required for task_kind=review")
            sys.exit(1)
        run_review(gl, llm, project_id, mr_iid, kind, note_id)

    elif task_kind in ("fix_issue", "fix_mr"):
        iid = issue_iid if task_kind == "fix_issue" else mr_iid
        if iid is None:
            logger.error("IID not set for task_kind=%s", task_kind)
            sys.exit(1)
        run_fix(gl, llm, ws, project_id, kind, iid, note_id, task_kind)

    else:
        logger.error("Unknown TASK_KIND: %r", task_kind)
        sys.exit(1)


if __name__ == "__main__":
    main()
