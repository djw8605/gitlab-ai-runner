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
from typing import Optional, TextIO

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

DEFAULT_OPENCODE_TIMEOUT_SECONDS = 1800
DEFAULT_OPENCODE_MAX_CONTEXT_TOKENS = 128000
DEFAULT_OPENCODE_MAX_OUTPUT_TOKENS = 100000
MAX_CONTEXT_NOTES = 30
MAX_NOTE_BODY_CHARS = 1200
MAX_NOTES_CONTEXT_CHARS = 16000
AGENT_STDIO_TAIL_LINES = 30


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


def _parse_int_env(name: str, default: int) -> int:
    raw = _optional(name, str(default))
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default %d", name, raw, default)
        return default


def _parse_int_env_any(names: tuple[str, ...], default: int) -> int:
    for name in names:
        raw = _optional(name, "")
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            logger.warning("Invalid %s=%r, ignoring", name, raw)
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
# OpenCode helpers
# ---------------------------------------------------------------------------


def _write_opencode_config(
    config_path: Path,
    *,
    base_url: str,
    model: str,
    api_key: str,
    max_context_tokens: int,
    max_output_tokens: int,
) -> None:
    """Write a project-local opencode.json for deterministic non-interactive runs."""
    provider_name = "custom"
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            provider_name: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Custom OpenAI-Compatible",
                "options": {
                    "baseURL": base_url,
                    "apiKey": api_key,
                },
                "models": {
                    model: {
                        "name": model,
                        "limit": {
                            "context": max_context_tokens,
                            "output": max_output_tokens,
                        },
                    }
                },
            }
        },
        "model": f"{provider_name}/{model}",
        "small_model": f"{provider_name}/{model}",
        # Never prompt for permissions in batch mode.
        "permission": "allow",
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote %s", config_path)


def _run_opencode(
    *,
    cwd: Path,
    prompt: str,
    model: str,
    config_path: Path,
    data_dir: Path,
    timeout_seconds: int,
) -> str:
    """Execute opencode in non-interactive mode, stream logs, and return stdout."""
    cmd = ["opencode", "run", "--model", f"custom/{model}", prompt]
    #display_cmd = [*cmd[:-1], "<prompt>"]
    logger.info("Running opencode in %s", cwd)
    logger.info("OpenCode command: %s", " ".join(cmd))

    env = os.environ.copy()
    home_dir = data_dir / "home"
    xdg_cache = data_dir / "xdg-cache"
    xdg_config = data_dir / "xdg-config"
    xdg_data = data_dir / "xdg-data"
    home_dir.mkdir(parents=True, exist_ok=True)
    xdg_cache.mkdir(parents=True, exist_ok=True)
    xdg_config.mkdir(parents=True, exist_ok=True)
    xdg_data.mkdir(parents=True, exist_ok=True)

    env["OPENCODE_CONFIG"] = str(config_path)
    env["HOME"] = str(home_dir)
    env["XDG_CACHE_HOME"] = str(xdg_cache)
    env["XDG_CONFIG_HOME"] = str(xdg_config)
    env["XDG_DATA_HOME"] = str(xdg_data)
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
        raise RuntimeError("opencode binary not found in runner image") from exc

    stdout_buf: list[str] = []
    stderr_buf: list[str] = []

    def _pump(stream: Optional[TextIO], prefix: str, buf: list[str]) -> None:
        if stream is None:
            return
        # line-buffered stream copy so agent output appears in pod logs.
        for line in iter(stream.readline, ""):
            buf.append(line)
            text = line.rstrip()
            if text:
                logger.info("opencode %s | %s", prefix, text)
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
        raise RuntimeError(f"opencode timed out after {timeout_seconds}s") from exc
    finally:
        out_thread.join()
        err_thread.join()

    stdout = "".join(stdout_buf).strip()
    stderr = "".join(stderr_buf).strip()
    logger.info("OpenCode exit code: %d", result_code)

    def _tail_lines(text: str, n: int = AGENT_STDIO_TAIL_LINES) -> str:
        if not text:
            return ""
        lines = text.splitlines()
        return "\n".join(lines[-n:])

    stdout_tail = _tail_lines(stdout)
    stderr_tail = _tail_lines(stderr)
    logger.info("OpenCode stdout tail:\n%s", stdout_tail or "<empty>")
    logger.info("OpenCode stderr tail:\n%s", stderr_tail or "<empty>")

    if result_code != 0:
        detail = stderr[-3000:] if stderr else stdout[-3000:]
        raise RuntimeError(f"opencode failed with exit code {result_code}: {detail}")

    if not stdout:
        raise RuntimeError("opencode returned an empty response")

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


def _log_post_agent_git_diagnostics(repo_dir: Path) -> None:
    """Log git status and diff summary after an agent run."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    status_out = (status.stdout or "").strip()
    logger.info(
        "Post-agent git status --porcelain:\n%s",
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
            logger.info("Post-agent git diff --stat:\n%s", diff_stat_out)


def run_review(
    gl: GitLabClient,
    *,
    project_id: int,
    mr_iid: int,
    opencode_user_prompt: str,
    opencode_model: str,
    opencode_config_path: Path,
    opencode_data_dir: Path,
    opencode_timeout_seconds: int,
    opencode_workdir: Path,
) -> None:
    """Fetch MR diff, produce a review via opencode, post as a note."""
    logger.info("Starting REVIEW task for MR !%d", mr_iid)

    mr = gl.get_mr(project_id, mr_iid)
    changes = gl.get_mr_changes(project_id, mr_iid)
    mr_notes = gl.get_mr_notes(project_id, mr_iid)

    title = mr.get("title", "")
    description = mr.get("description", "")
    diff_text = _format_diff(changes)
    notes_context = _format_notes_context(mr_notes)
    user_prompt = opencode_user_prompt or "(none)"

    prompt = textwrap.dedent(
        f"""\
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
        review_text = _run_opencode(
            cwd=opencode_workdir,
            prompt=prompt,
            model=opencode_model,
            config_path=opencode_config_path,
            data_dir=opencode_data_dir,
            timeout_seconds=opencode_timeout_seconds,
        )
    except RuntimeError as exc:
        logger.error("OpenCode review failed: %s", exc)
        gl.post_mr_note(
            project_id,
            mr_iid,
            f"⚠️ **OpenCode**: review failed.\n\n```\n{exc}\n```",
        )
        sys.exit(1)

    note_body = f"## 🤖 OpenCode Review\n\n{review_text}"
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
    opencode_user_prompt: str,
    opencode_model: str,
    opencode_config_path: Path,
    opencode_data_dir: Path,
    opencode_timeout_seconds: int,
    precreated_mr_iid: Optional[int] = None,
    precreated_mr_url: str = "",
    precreated_mr_branch: str = "",
    precreated_mr_target_branch: str = "",
) -> None:
    """Fix an issue or MR: let opencode edit code, push branch, open MR."""
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
            f"This MR was automatically generated by OpenCode in response to "
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
            f"This MR was automatically generated by OpenCode in response to "
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
    user_prompt = opencode_user_prompt or "(none)"
    prompt = textwrap.dedent(
        f"""\
        You are operating inside a Linux container with a git repository.

        ⚠️⚠️⚠️ CRITICAL EXECUTION AND VALIDATION REQUIREMENTS ⚠️⚠️⚠️

        READ THIS CAREFULLY: The task is NOT complete until ALL steps are done and validation PASSES.
        You MUST complete this ENTIRE workflow. DO NOT exit or stop until validation succeeds.

        ═══════════════════════════════════════════════════════════════════════════════
        MANDATORY WORKFLOW - EXECUTE EVERY STEP IN ORDER:
        ═══════════════════════════════════════════════════════════════════════════════

        STEP 1: ANALYZE THE PROJECT
        ----------------------------
           - Examine package.json, requirements.txt, Cargo.toml, go.mod, or other dependency files
           - Identify the project type (Node.js, Python, Go, Rust, etc.)
           - Check for build commands in package.json, Makefile, or CI configs
           - Note: If creating new directories (e.g., frontend/), remember to cd into them

        STEP 2: IMPLEMENT THE REQUESTED CHANGES
        ----------------------------------------
           - Make concrete file edits to address the task
           - Follow existing code patterns and style
           - Fix any syntax errors you create IMMEDIATELY

        STEP 3: INSTALL DEPENDENCIES (ABSOLUTELY MANDATORY - NO EXCEPTIONS)
        --------------------------------------------------------------------
           Python projects:
           → cd to the directory with requirements.txt
           → Run: pip install -r requirements.txt
           
           Node.js/Next.js projects:
           → cd to the directory with package.json
           → Run: npm install
           → If you created a new frontend/ directory, run: cd frontend && npm install
           
           Go projects:
           → Run: go mod download
           
           Rust projects:
           → Run: cargo fetch
           
           ⛔ NEVER SKIP DEPENDENCY INSTALLATION ⛔
           Even if you think deps are installed, ALWAYS run the install command.

        STEP 4: RUN SMOKE TESTS (ABSOLUTELY MANDATORY - NO EXCEPTIONS)
        ---------------------------------------------------------------
           You MUST run validation commands and show their output. Choose appropriate commands:
           
           For Node.js/Next.js projects (EXECUTE ALL OF THESE):
           → cd to the directory with package.json
           → Run: npm run build
           → Check if build succeeded (exit code 0)
           → If TypeScript errors appear, FIX THEM and rerun npm run build
           → Show the full build output
           
           For Python projects:
           → Run: python -m pytest (if tests exist)
           → Run: python -m py_compile <changed_files>
           → Run: python -c "import module_name" to verify imports
           
           For Go projects:
           → Run: go build ./...
           → Run: go test ./... (if tests exist)
           
           For Docker projects:
           → Run: docker build -f Dockerfile .
           
           ⛔ DO NOT PROCEED WITHOUT RUNNING AND SHOWING VALIDATION OUTPUT ⛔

        STEP 5: FIX ALL ERRORS - ITERATE UNTIL SUCCESSFUL
        --------------------------------------------------
           If validation fails (TypeScript errors, build errors, test failures, etc.):
           
           🔄 REQUIRED FIX LOOP:
           a) Read and analyze the error message carefully
           b) Identify the root cause (missing types, syntax errors, wrong imports, etc.)
           c) Make the necessary fix to the code
           d) Rerun the exact same validation command
           e) Verify it now passes (exit code 0, no errors)
           f) If it still fails, repeat steps a-e until it succeeds
           
           Example fix cycle for TypeScript errors:
           ```
           $ npm run build
           ERROR: Type 'string | undefined' is not assignable to type 'string'
           
           [Fix the type error in the code]
           
           $ npm run build
           ✓ Compiled successfully
           ```
           
           ⛔ DO NOT SAY "Let me fix it" AND THEN EXIT ⛔
           ⛔ ACTUALLY FIX IT AND RERUN THE COMMAND ⛔

        STEP 6: REPORT VALIDATION SUCCESS
        ----------------------------------
           After ALL validation passes:
           - Show the successful command output
           - Confirm all builds/tests passed
           - List what you validated (e.g., "Ran npm run build successfully, no TypeScript errors")
           
           If validation is blocked (missing network/services):
           - State EXACTLY what is blocking validation
           - State what would be needed to validate
           - This is ONLY acceptable if truly impossible (not just inconvenient)

        ═══════════════════════════════════════════════════════════════════════════════
        ❌ THINGS THAT WILL CAUSE TASK FAILURE ❌
        ═══════════════════════════════════════════════════════════════════════════════
        
        ✗ Skipping dependency installation
        ✗ Skipping smoke tests
        ✗ Acknowledging errors exist but not fixing them
        ✗ Saying "let me fix it" and then exiting
        ✗ Running validation commands that fail and not iterating to fix them
        ✗ Exiting before showing successful validation output
        ✗ Assuming validation will pass without actually running commands

        ═══════════════════════════════════════════════════════════════════════════════
        ✅ TASK COMPLETION CHECKLIST ✅
        ═══════════════════════════════════════════════════════════════════════════════
        
        Before you finish, verify you have:
        □ Installed all dependencies (npm install, pip install, etc.)
        □ Run at least one validation command (build/test/compile)
        □ Fixed ALL errors that appeared during validation
        □ Rerun validation to confirm errors are fixed
        □ Shown the successful validation output
        □ Verified exit codes are 0 for all validation commands
        
        If ANY checkbox is unchecked, you are NOT done. Continue working.

        ═══════════════════════════════════════════════════════════════════════════════
        PROJECT CONTEXT:
        ═══════════════════════════════════════════════════════════════════════════════
        
        Project: {path_with_namespace}
        Task kind: {task_kind}
        Target: {back_ref}
        Title: {item_title}

        Description:
        {item_description}

        Additional Prompt from Trigger Comment (everything after @crush):
        {user_prompt}

        {context_label}:
        {notes_context}
        """
    )

    try:
        agent_summary = _run_opencode(
            cwd=ws.repo_dir,
            prompt=prompt,
            model=opencode_model,
            config_path=opencode_config_path,
            data_dir=opencode_data_dir,
            timeout_seconds=opencode_timeout_seconds,
        )
    except RuntimeError as exc:
        logger.error("OpenCode fix failed: %s", exc)
        _log_post_agent_git_diagnostics(ws.repo_dir)
        gl.post_note(
            project_id,
            kind,
            iid,
            f"⚠️ **OpenCode**: automated fix failed.\n\n```\n{exc}\n```",
        )
        sys.exit(1)

    _log_post_agent_git_diagnostics(ws.repo_dir)

    if not ws.has_changes():
        msg = "No filesystem changes detected."
        logger.error(msg)
        gl.post_note(project_id, kind, iid, f"⚠️ **OpenCode**: {msg}")
        sys.exit(1)

    commit_msg = f"chore: OpenCode automated fix for {back_ref}\n\nTask: {item_title}"
    ws.commit_all(commit_msg)

    if not ws.has_changes() and not _branch_has_commits(ws, base_branch, new_branch):
        msg = (
            "No filesystem changes detected and no commits were produced."
        )
        logger.error(msg)
        gl.post_note(project_id, kind, iid, f"⚠️ **OpenCode**: {msg}")
        sys.exit(1)

    passed, test_output = ws.run_tests()
    if not passed:
        test_snippet = test_output[-3000:] if len(test_output) > 3000 else test_output
        gl.post_note(
            project_id,
            kind,
            iid,
            f"⚠️ **OpenCode**: tests failed after applying changes. "
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
            agent_summary[-2000:] if len(agent_summary) > 2000 else agent_summary
        )
        gl.post_issue_note(
            project_id,
            iid,
            f"🤖 **OpenCode** updated merge request {mr_ref}.\n\n"
            f"Branch: `{new_branch}`",
        )
        if precreated_mr_iid is not None:
            gl.post_mr_note(
                project_id,
                precreated_mr_iid,
                "🤖 **OpenCode** updated this merge request from the linked issue.\n\n"
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
            f"⚠️ **OpenCode**: branch `{new_branch}` was pushed but MR creation failed.\n"
            f"Please open the MR manually.\n\n```\n{exc}\n```",
        )
        sys.exit(1)

    summary_tail = agent_summary[-2000:] if len(agent_summary) > 2000 else agent_summary
    gl.post_note(
        project_id,
        kind,
        iid,
        f"🤖 **OpenCode** created a fix in !{new_mr_iid}: {new_mr_url}\n\n"
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
    opencode_user_prompt = _optional("OPENCODE_USER_PROMPT", _optional("CRUSH_USER_PROMPT"))

    opencode_base_url = _require_any("OPENCODE_BASE_URL", "CRUSH_BASE_URL", "LLM_BASE_URL")
    opencode_model = _require_any("OPENCODE_MODEL", "CRUSH_MODEL", "LLM_MODEL")
    opencode_api_key = _require_any("OPENCODE_API_KEY", "CRUSH_API_KEY", "LLM_API_KEY")
    opencode_timeout_seconds = _parse_int_env_any(
        ("OPENCODE_TIMEOUT_SECONDS", "CRUSH_TIMEOUT_SECONDS"),
        DEFAULT_OPENCODE_TIMEOUT_SECONDS,
    )
    opencode_max_context_tokens = _parse_int_env_any(
        ("OPENCODE_MAX_CONTEXT_TOKENS",),
        DEFAULT_OPENCODE_MAX_CONTEXT_TOKENS,
    )
    if opencode_max_context_tokens < 1:
        logger.warning(
            "Invalid OPENCODE_MAX_CONTEXT_TOKENS=%d, using default %d",
            opencode_max_context_tokens,
            DEFAULT_OPENCODE_MAX_CONTEXT_TOKENS,
        )
        opencode_max_context_tokens = DEFAULT_OPENCODE_MAX_CONTEXT_TOKENS
    opencode_max_output_tokens = _parse_int_env_any(
        ("OPENCODE_MAX_OUTPUT_TOKENS", "CRUSH_MAX_TOKENS"),
        DEFAULT_OPENCODE_MAX_OUTPUT_TOKENS,
    )
    if opencode_max_output_tokens < 1:
        logger.warning(
            "Invalid OPENCODE_MAX_OUTPUT_TOKENS=%d, using default %d",
            opencode_max_output_tokens,
            DEFAULT_OPENCODE_MAX_OUTPUT_TOKENS,
        )
        opencode_max_output_tokens = DEFAULT_OPENCODE_MAX_OUTPUT_TOKENS

    gl = GitLabClient(
        base_url=_require("GITLAB_BASE_URL"),
        token=_require("GITLAB_TOKEN"),
    )

    workspace_root = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
    workspace_root.mkdir(parents=True, exist_ok=True)

    opencode_config_path = workspace_root / ".opencode-runner-config" / "opencode.json"
    opencode_data_dir = workspace_root / ".opencode-runner-data"
    opencode_data_dir.mkdir(parents=True, exist_ok=True)

    _write_opencode_config(
        opencode_config_path,
        base_url=opencode_base_url,
        model=opencode_model,
        api_key=opencode_api_key,
        max_context_tokens=opencode_max_context_tokens,
        max_output_tokens=opencode_max_output_tokens,
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
            opencode_user_prompt=opencode_user_prompt,
            opencode_model=opencode_model,
            opencode_config_path=opencode_config_path,
            opencode_data_dir=opencode_data_dir,
            opencode_timeout_seconds=opencode_timeout_seconds,
            opencode_workdir=workspace_root,
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
            opencode_user_prompt=opencode_user_prompt,
            opencode_model=opencode_model,
            opencode_config_path=opencode_config_path,
            opencode_data_dir=opencode_data_dir,
            opencode_timeout_seconds=opencode_timeout_seconds,
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
