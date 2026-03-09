"""Runner job entrypoint.

This script is executed inside the Kubernetes Job container.
It reads environment variables, performs the requested task (review or fix),
and posts results back to GitLab.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Optional

from gitlab import GitLabClient, GitLabError
from workspace import MAX_DIFF_CHARS, Workspace

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("runner")

DEFAULT_CRUSH_ALLOWED_TOOLS = "view,ls,grep,edit,bash"
DEFAULT_CRUSH_TIMEOUT_SECONDS = 1800
DEFAULT_CRUSH_MAX_TOKENS = 4096
DEFAULT_CRUSH_EXECUTION_ANCHOR_FILE = ".crush/last_run.txt"
MAX_CONTEXT_NOTES = 30
MAX_NOTE_BODY_CHARS = 1200
MAX_NOTES_CONTEXT_CHARS = 16000
CRUSH_STDIO_TAIL_LINES = 30
NO_FILE_EDITS_MARKER = "NO_FILE_EDITS_NEEDED:"
PLANNING_LOOP_MARKERS = (
    "## current state",
    "## files & changes",
    "## todo list status",
    "for the resuming assistant",
    "todos tool",
    "## technical context",
    "## strategy & approach",
    "## exact next steps",
)


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


def _require_any(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    logger.error("Required env var is missing; expected one of: %s", ", ".join(names))
    sys.exit(1)


def _parse_allowed_tools(raw: str) -> list[str]:
    tools = [tool.strip() for tool in raw.split(",") if tool.strip()]
    if not tools:
        tools = [tool.strip() for tool in DEFAULT_CRUSH_ALLOWED_TOOLS.split(",")]
    deduped: list[str] = []
    seen: set[str] = set()
    for tool in tools:
        if tool not in seen:
            deduped.append(tool)
            seen.add(tool)
    return deduped


def _parse_int_env(name: str, default: int) -> int:
    raw = _optional(name, str(default))
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default %d", name, raw, default)
        return default


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated] ..."


def _format_notes_context(notes: list[dict]) -> str:
    """Render issue/MR notes into compact context for prompts."""
    if not notes:
        return "None."

    entries: list[str] = []
    for note in notes[-MAX_CONTEXT_NOTES:]:
        if note.get("system"):
            continue
        author = (note.get("author") or {}).get("username", "unknown")
        body = str(note.get("body", "")).strip()
        if not body:
            continue
        body = _truncate(body, MAX_NOTE_BODY_CHARS)
        entries.append(f"[{author}]\n{body}")

    if not entries:
        return "None."

    merged = "\n\n---\n\n".join(entries)
    return _truncate(merged, MAX_NOTES_CONTEXT_CHARS)


# ---------------------------------------------------------------------------
# Crush helpers
# ---------------------------------------------------------------------------


def _write_crush_config(
    config_path: Path,
    *,
    base_url: str,
    model: str,
    api_key: str,
    allowed_tools: list[str],
    max_tokens: int,
) -> None:
    """Write a project-local crush.json so non-interactive runs are deterministic."""
    cfg = {
        "$schema": "https://charm.land/crush.json",
        "providers": {
            "local": {
                "name": "Local OpenAI-Compatible",
                "type": "openai-compat",
                "base_url": base_url,
                "api_key": api_key,
                "models": [
                    {
                        "id": model,
                        "name": model,
                    }
                ],
            }
        },
        "models": {
            "large": {
                "provider": "local",
                "model": model,
                "max_tokens": max_tokens,
            },
            "small": {
                "provider": "local",
                "model": model,
                "max_tokens": max_tokens,
            },
        },
        "permissions": {
            "allowed_tools": allowed_tools,
        },
        "options": {
            "disable_metrics": True,
        },
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote %s", config_path)


def _run_crush(
    *,
    cwd: Path,
    prompt: str,
    model: str,
    config_path: Path,
    data_dir: Path,
    timeout_seconds: int,
) -> str:
    """Execute crush in non-interactive mode, stream logs, and return stdout."""
    cmd = ["crush", "-c", str(cwd), "-D", str(data_dir), "run", prompt]
    display_cmd = [*cmd[:-1], "<prompt>"]
    logger.info("Running crush in %s", cwd)
    logger.info("Crush command: %s", " ".join(display_cmd))

    env = os.environ.copy()
    crush_home = data_dir / "home"
    xdg_cache = data_dir / "xdg-cache"
    xdg_config = data_dir / "xdg-config"
    xdg_data = data_dir / "xdg-data"
    crush_home.mkdir(parents=True, exist_ok=True)
    xdg_cache.mkdir(parents=True, exist_ok=True)
    xdg_config.mkdir(parents=True, exist_ok=True)
    xdg_data.mkdir(parents=True, exist_ok=True)

    env["CRUSH_DISABLE_METRICS"] = "1"
    # CRUSH_GLOBAL_CONFIG expects a directory; crush appends "/crush.json".
    env["CRUSH_GLOBAL_CONFIG"] = str(config_path.parent)
    env["CRUSH_GLOBAL_DATA"] = str(data_dir)
    env["HOME"] = str(crush_home)
    env["XDG_CACHE_HOME"] = str(xdg_cache)
    env["XDG_CONFIG_HOME"] = str(xdg_config)
    env["XDG_DATA_HOME"] = str(xdg_data)
    env.setdefault("CRUSH_DISABLE_PROVIDER_AUTO_UPDATE", "1")
    env.setdefault("DEBIAN_FRONTEND", "noninteractive")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("crush binary not found in runner image") from exc

    stdout_buf: list[str] = []
    stderr_buf: list[str] = []
    planning_violation: Optional[str] = None

    def _pump(stream: Optional[object], prefix: str, buf: list[str]) -> None:
        nonlocal planning_violation
        if stream is None:
            return
        # line-buffered stream copy so crush output appears in pod logs.
        for line in iter(stream.readline, ""):
            buf.append(line)
            text = line.rstrip()
            if text:
                logger.info("crush %s | %s", prefix, text)
                if prefix == "stdout" and planning_violation is None:
                    lowered = text.lower()
                    for marker in PLANNING_LOOP_MARKERS:
                        if marker in lowered:
                            planning_violation = marker
                            logger.error(
                                "Detected planning-only crush output marker %r; terminating run",
                                marker,
                            )
                            if proc.poll() is None:
                                proc.terminate()
                            break
        stream.close()

    out_thread = threading.Thread(
        target=_pump, args=(proc.stdout, "stdout", stdout_buf), daemon=True
    )
    err_thread = threading.Thread(
        target=_pump, args=(proc.stderr, "stderr", stderr_buf), daemon=True
    )
    out_thread.start()
    err_thread.start()

    try:
        result_code = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise RuntimeError(f"crush timed out after {timeout_seconds}s") from exc
    finally:
        out_thread.join()
        err_thread.join()

    stdout = "".join(stdout_buf).strip()
    stderr = "".join(stderr_buf).strip()
    logger.info("Crush exit code: %d", result_code)

    def _tail_lines(text: str, n: int = CRUSH_STDIO_TAIL_LINES) -> str:
        if not text:
            return ""
        lines = text.splitlines()
        return "\n".join(lines[-n:])

    stdout_tail = _tail_lines(stdout)
    stderr_tail = _tail_lines(stderr)
    logger.info("Crush stdout tail:\n%s", stdout_tail or "<empty>")
    logger.info("Crush stderr tail:\n%s", stderr_tail or "<empty>")

    if planning_violation is not None:
        detail = stderr[-3000:] if stderr else stdout[-3000:]
        raise RuntimeError(
            "crush produced planning-only output "
            f"(marker: {planning_violation!r}) and was terminated. "
            f"Output tail:\n{detail}"
        )

    if result_code != 0:
        detail = stderr[-3000:] if stderr else stdout[-3000:]
        raise RuntimeError(f"crush failed with exit code {result_code}: {detail}")

    if not stdout:
        raise RuntimeError("crush returned an empty response")

    return stdout


# ---------------------------------------------------------------------------
# Review task
# ---------------------------------------------------------------------------


def _format_diff(changes: dict) -> str:
    """Extract and truncate the diff text from MR changes."""
    diff_parts: list[str] = []
    for change in changes.get("changes", []):
        path = change.get("new_path") or change.get("old_path", "?")
        diff = change.get("diff", "")
        diff_parts.append(f"--- {path} ---\n{diff}")
    full = "\n".join(diff_parts)
    if len(full) > MAX_DIFF_CHARS:
        full = full[:MAX_DIFF_CHARS] + "\n... [diff truncated] ..."
    return full


def _log_post_crush_git_diagnostics(repo_dir: Path) -> None:
    """Log git status and diff summary after a crush run."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    status_out = (status.stdout or "").strip()
    logger.info(
        "Post-crush git status --porcelain:\n%s",
        status_out if status_out else "<clean>",
    )

    if status_out:
        diff_stat = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        diff_stat_out = (diff_stat.stdout or "").strip()
        if diff_stat_out:
            logger.info("Post-crush git diff --stat:\n%s", diff_stat_out)


def run_review(
    gl: GitLabClient,
    *,
    project_id: int,
    mr_iid: int,
    crush_user_prompt: str,
    crush_model: str,
    crush_config_path: Path,
    crush_data_dir: Path,
    crush_timeout_seconds: int,
    crush_workdir: Path,
) -> None:
    """Fetch MR diff, produce a review via crush, post as a note."""
    logger.info("Starting REVIEW task for MR !%d", mr_iid)

    mr = gl.get_mr(project_id, mr_iid)
    changes = gl.get_mr_changes(project_id, mr_iid)
    mr_notes = gl.get_mr_notes(project_id, mr_iid)

    title = mr.get("title", "")
    description = mr.get("description", "")
    diff_text = _format_diff(changes)
    notes_context = _format_notes_context(mr_notes)
    user_prompt = crush_user_prompt or "(none)"

    prompt = textwrap.dedent(
        f"""\
        You are an expert code reviewer.

        Review the following GitLab merge request and produce Markdown with these exact sections:
        - ## Summary
        - ## Major Issues
        - ## Minor Issues
        - ## Suggested Tests
        - ## Security Notes

        Be specific and include file names/line numbers when relevant.
        If a section has no findings, write: None identified.

        MR Title: {title}

        MR Description:
        {description}

        Additional Prompt from Trigger Comment (everything after @crush):
        {user_prompt}

        Merge Request Comment Context:
        {notes_context}

        Diff:
        ```diff
        {diff_text}
        ```
        """
    )

    try:
        review_text = _run_crush(
            cwd=crush_workdir,
            prompt=prompt,
            model=crush_model,
            config_path=crush_config_path,
            data_dir=crush_data_dir,
            timeout_seconds=crush_timeout_seconds,
        )
    except RuntimeError as exc:
        logger.error("Crush review failed: %s", exc)
        gl.post_mr_note(
            project_id,
            mr_iid,
            f"⚠️ **Crush**: review failed.\n\n```\n{exc}\n```",
        )
        sys.exit(1)

    note_body = f"## 🤖 Crush Code Review\n\n{review_text}"
    gl.post_mr_note(project_id, mr_iid, note_body)
    logger.info("Posted review to MR !%d", mr_iid)


# ---------------------------------------------------------------------------
# Fix task (shared for issue and MR-fix)
# ---------------------------------------------------------------------------


def run_fix(
    gl: GitLabClient,
    ws: Workspace,
    *,
    project_id: int,
    kind: str,
    iid: int,
    task_kind: str,
    crush_user_prompt: str,
    crush_model: str,
    crush_config_path: Path,
    crush_data_dir: Path,
    crush_timeout_seconds: int,
    crush_execution_anchor_file: str,
    precreated_mr_iid: Optional[int] = None,
    precreated_mr_url: str = "",
    precreated_mr_branch: str = "",
    precreated_mr_target_branch: str = "",
) -> None:
    """Fix an issue or MR: let crush edit code, push branch, open MR."""
    logger.info("Starting FIX task (%s) for %s #%d", task_kind, kind, iid)

    project = gl.get_project(project_id)
    path_with_namespace: str = project["path_with_namespace"]
    default_branch: str = project.get("default_branch", "main")
    gitlab_base_url = os.environ.get("GITLAB_BASE_URL", "")
    gitlab_token = os.environ.get("GITLAB_TOKEN", "")
    item_notes: list[dict] = []
    use_precreated_issue_mr = (
        task_kind == "fix_issue"
        and bool(precreated_mr_branch)
        and precreated_mr_iid is not None
    )

    if task_kind == "fix_issue":
        item = gl.get_issue(project_id, iid)
        item_notes = gl.get_issue_notes(project_id, iid)
        item_title: str = item.get("title", f"Issue #{iid}")
        item_description: str = item.get("description", "")
        if use_precreated_issue_mr:
            base_branch = precreated_mr_target_branch or default_branch
            new_branch = precreated_mr_branch
        else:
            base_branch = default_branch
            new_branch = ws.issue_branch(iid, item_title)
        back_ref = f"issue #{iid}"
        context_label = "Issue Comment Context"
        mr_title = f"fix: resolve issue #{iid} - {item_title}"
        mr_description = (
            f"Closes #{iid}\n\n"
            f"This MR was automatically generated by Crush in response to "
            f"[issue #{iid}]({item.get('web_url', '')})."
        )
    else:  # fix_mr
        item = gl.get_mr(project_id, iid)
        item_notes = gl.get_mr_notes(project_id, iid)
        item_title = item.get("title", f"MR !{iid}")
        item_description = item.get("description", "")
        base_branch = item.get("target_branch", default_branch)
        new_branch = ws.mr_fix_branch(iid)
        back_ref = f"MR !{iid}"
        context_label = "Merge Request Comment Context"
        mr_title = f"fix: address changes requested in !{iid}"
        mr_description = (
            f"This MR was automatically generated by Crush in response to "
            f"[MR !{iid}]({item.get('web_url', '')})."
        )

    ws.clone(
        gitlab_base_url=gitlab_base_url,
        path_with_namespace=path_with_namespace,
        token=gitlab_token,
        branch=base_branch,
    )
    if use_precreated_issue_mr:
        ws.checkout_remote_branch(new_branch)
    else:
        ws.create_branch(new_branch)

    notes_context = _format_notes_context(item_notes)
    user_prompt = crush_user_prompt or "(none)"
    anchor_file = crush_execution_anchor_file.strip()
    if anchor_file:
        first_action = textwrap.dedent(
            f"""\
            - Create or update `{anchor_file}` and ensure `.crush/` is listed in `.gitignore`.
            - Write one line containing task identifiers: task kind, target, and timestamp.
            - If you cannot edit files for some reason, run `pwd && ls -la` and stop with the error.
            """
        ).strip()
    else:
        first_action = (
            "- Run `pwd && ls -la` as your first action to prove tool execution is active."
        )

    prompt = textwrap.dedent(
        f"""\
        You are operating inside a Linux container with a git repo.

        Execution policy:
        - You must start by using a tool (bash or edit). No pure-text responses.
        - Prefer making a small, working change in this run.
        - If the task is large, complete a Phase 1 slice and stop.
        - Do not restate architecture.
        - Do not produce long plans or todo lists.
        - Every step must run a shell command or edit a file.
        - No external tool APIs are available (do not reference todos/tools).

        Task kind: {task_kind}
        Project: {path_with_namespace}
        Target: {back_ref}
        Title: {item_title}

        Description:
        {item_description}

        Additional Prompt from Trigger Comment (everything after @crush):
        {user_prompt}

        {context_label}:
        {notes_context}

        Safety and correctness:
        - If a command fails, print the exact command and error output, then stop.
        - Do not commit or push.

        Tooling:
        - Available tools: bash, edit, view, grep, ls.
        - Do not install system packages unless the task cannot proceed without them.
        - Prefer repository-local approaches first.
        - If system install is required, explain why, then install minimally.
        - If install fails due to permissions/network, continue with file-only changes and document next steps.

        Mandatory first action:
        {first_action}

        Instructions:
        - Make the smallest working change that moves the task forward.
        - If the task is broad, implement only a Phase 1 slice in this run (minimal scaffolding or one working path), then stop.
        - Edit files directly in this working tree.
        - For large tasks, leave clear TODO comments in code for subsequent phases.
        - Run relevant tests only if available and quick; otherwise skip and note it.
        - If no file edits are needed, still run at least one safe command and include:
          {NO_FILE_EDITS_MARKER} <reason>
        - Finish by printing a short Markdown summary with sections:
          ## Summary
          ## Files Changed
          ## Tests Run
        """
    )

    try:
        crush_summary = _run_crush(
            cwd=ws.repo_dir,
            prompt=prompt,
            model=crush_model,
            config_path=crush_config_path,
            data_dir=crush_data_dir,
            timeout_seconds=crush_timeout_seconds,
        )
    except RuntimeError as exc:
        logger.error("Crush fix failed: %s", exc)
        _log_post_crush_git_diagnostics(ws.repo_dir)
        gl.post_note(
            project_id,
            kind,
            iid,
            f"⚠️ **Crush**: automated fix failed.\n\n```\n{exc}\n```",
        )
        sys.exit(1)

    _log_post_crush_git_diagnostics(ws.repo_dir)

    if not ws.has_changes():
        if NO_FILE_EDITS_MARKER in crush_summary:
            logger.info("No file edits were required for this run")
            gl.post_note(
                project_id,
                kind,
                iid,
                f"ℹ️ **Crush**: No file edits required.\n\n### Runner Summary\n\n{crush_summary[-2000:]}",
            )
            return
        msg = (
            "No filesystem changes detected and no explicit no-edit justification was provided."
        )
        logger.error(msg)
        gl.post_note(project_id, kind, iid, f"⚠️ **Crush**: {msg}")
        sys.exit(1)

    commit_msg = f"chore: Crush automated fix for {back_ref}\n\nTask: {item_title}"
    ws.commit_all(commit_msg)

    if not ws.has_changes() and not _branch_has_commits(ws, base_branch, new_branch):
        msg = (
            "No filesystem changes detected and no commits were produced."
        )
        logger.error(msg)
        gl.post_note(project_id, kind, iid, f"⚠️ **Crush**: {msg}")
        sys.exit(1)

    passed, test_output = ws.run_tests()
    if not passed:
        test_snippet = test_output[-3000:] if len(test_output) > 3000 else test_output
        gl.post_note(
            project_id,
            kind,
            iid,
            f"⚠️ **Crush**: tests failed after applying changes. "
            f"Branch `{new_branch}` was NOT pushed.\n\n```\n{test_snippet}\n```",
        )
        sys.exit(1)

    ws.push(new_branch)

    if use_precreated_issue_mr:
        if not precreated_mr_url and precreated_mr_iid is not None:
            mr = gl.get_mr(project_id, precreated_mr_iid)
            precreated_mr_url = mr.get("web_url", "")
        mr_ref = (
            f"[!{precreated_mr_iid}]({precreated_mr_url})"
            if precreated_mr_iid is not None and precreated_mr_url
            else f"!{precreated_mr_iid}"
        )
        summary_tail = (
            crush_summary[-2000:] if len(crush_summary) > 2000 else crush_summary
        )
        gl.post_issue_note(
            project_id,
            iid,
            f"🤖 **Crush** updated merge request {mr_ref}.\n\n"
            f"Branch: `{new_branch}`",
        )
        if precreated_mr_iid is not None:
            gl.post_mr_note(
                project_id,
                precreated_mr_iid,
                "🤖 **Crush** updated this merge request from the linked issue.\n\n"
                f"### Runner Summary\n\n{summary_tail}",
            )
        logger.info("Fix complete. Updated existing MR: %s", precreated_mr_url)
        return

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
            f"⚠️ **Crush**: branch `{new_branch}` was pushed but MR creation failed.\n"
            f"Please open the MR manually.\n\n```\n{exc}\n```",
        )
        sys.exit(1)

    summary_tail = crush_summary[-2000:] if len(crush_summary) > 2000 else crush_summary
    gl.post_note(
        project_id,
        kind,
        iid,
        f"🤖 **Crush** created a fix in !{new_mr_iid}: {new_mr_url}\n\n"
        f"Branch: `{new_branch}`\n\n"
        f"### Runner Summary\n\n{summary_tail}",
    )
    logger.info("Fix complete. New MR: %s", new_mr_url)


def _branch_has_commits(ws: Workspace, base_branch: str, new_branch: str) -> bool:
    """Return True if new_branch has commits ahead of base_branch."""
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

    mr_iid_str = _optional("MR_IID")
    issue_iid_str = _optional("ISSUE_IID")
    mr_iid: Optional[int] = int(mr_iid_str) if mr_iid_str else None
    issue_iid: Optional[int] = int(issue_iid_str) if issue_iid_str else None
    precreated_mr_iid_str = _optional("PRECREATED_MR_IID")
    precreated_mr_iid: Optional[int] = (
        int(precreated_mr_iid_str) if precreated_mr_iid_str else None
    )
    precreated_mr_url = _optional("PRECREATED_MR_URL")
    precreated_mr_branch = _optional("PRECREATED_MR_BRANCH")
    precreated_mr_target_branch = _optional("PRECREATED_MR_TARGET_BRANCH")
    crush_user_prompt = _optional("CRUSH_USER_PROMPT")
    crush_execution_anchor_file = _optional(
        "CRUSH_EXECUTION_ANCHOR_FILE", DEFAULT_CRUSH_EXECUTION_ANCHOR_FILE
    )

    crush_base_url = _require_any("CRUSH_BASE_URL", "LLM_BASE_URL")
    crush_model = _require_any("CRUSH_MODEL", "LLM_MODEL")
    crush_api_key = _require_any("CRUSH_API_KEY", "LLM_API_KEY")
    crush_allowed_tools = _parse_allowed_tools(
        _optional("CRUSH_ALLOWED_TOOLS", DEFAULT_CRUSH_ALLOWED_TOOLS)
    )
    crush_timeout_seconds = _parse_int_env(
        "CRUSH_TIMEOUT_SECONDS", DEFAULT_CRUSH_TIMEOUT_SECONDS
    )
    crush_max_tokens = _parse_int_env("CRUSH_MAX_TOKENS", DEFAULT_CRUSH_MAX_TOKENS)
    if crush_max_tokens < 1:
        logger.warning(
            "Invalid CRUSH_MAX_TOKENS=%d, using default %d",
            crush_max_tokens,
            DEFAULT_CRUSH_MAX_TOKENS,
        )
        crush_max_tokens = DEFAULT_CRUSH_MAX_TOKENS

    gl = GitLabClient(
        base_url=_require("GITLAB_BASE_URL"),
        token=_require("GITLAB_TOKEN"),
    )

    workspace_root = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
    workspace_root.mkdir(parents=True, exist_ok=True)

    crush_config_path = workspace_root / ".crush-runner-config" / "crush.json"
    crush_data_dir = workspace_root / ".crush-runner-data"
    crush_data_dir.mkdir(parents=True, exist_ok=True)

    # Ensure crush has provider/tool config available for both review and fix.
    _write_crush_config(
        crush_config_path,
        base_url=crush_base_url,
        model=crush_model,
        api_key=crush_api_key,
        allowed_tools=crush_allowed_tools,
        max_tokens=crush_max_tokens,
    )

    ws = Workspace(workspace_root)

    if task_kind == "review":
        if mr_iid is None:
            logger.error("MR_IID is required for task_kind=review")
            sys.exit(1)
        run_review(
            gl,
            project_id=project_id,
            mr_iid=mr_iid,
            crush_user_prompt=crush_user_prompt,
            crush_model=crush_model,
            crush_config_path=crush_config_path,
            crush_data_dir=crush_data_dir,
            crush_timeout_seconds=crush_timeout_seconds,
            crush_workdir=workspace_root,
        )

    elif task_kind in ("fix_issue", "fix_mr"):
        iid = issue_iid if task_kind == "fix_issue" else mr_iid
        if iid is None:
            logger.error("IID not set for task_kind=%s", task_kind)
            sys.exit(1)
        run_fix(
            gl,
            ws,
            project_id=project_id,
            kind=kind,
            iid=iid,
            task_kind=task_kind,
            crush_user_prompt=crush_user_prompt,
            crush_model=crush_model,
            crush_config_path=crush_config_path,
            crush_data_dir=crush_data_dir,
            crush_timeout_seconds=crush_timeout_seconds,
            crush_execution_anchor_file=crush_execution_anchor_file,
            precreated_mr_iid=precreated_mr_iid,
            precreated_mr_url=precreated_mr_url,
            precreated_mr_branch=precreated_mr_branch,
            precreated_mr_target_branch=precreated_mr_target_branch,
        )

    else:
        logger.error("Unknown TASK_KIND: %r", task_kind)
        sys.exit(1)


if __name__ == "__main__":
    main()
