"""Runner job entrypoint.

This script is executed inside the Kubernetes Job container.
It reads environment variables, performs the requested task (review or fix),
and posts results back to GitLab.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
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

DEFAULT_CODING_AGENT = "opencode"
SUPPORTED_CODING_AGENTS = {"opencode", "aider", "kilo"}
DEFAULT_AGENT_TIMEOUT_SECONDS = 1800
DEFAULT_AGENT_MAX_CONTEXT_TOKENS = 128000
DEFAULT_AGENT_MAX_OUTPUT_TOKENS = 100000
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


def _parse_coding_agent(raw: str) -> str:
    agent = raw.strip().lower() if raw else DEFAULT_CODING_AGENT
    if not agent:
        agent = DEFAULT_CODING_AGENT
    if agent not in SUPPORTED_CODING_AGENTS:
        supported = ", ".join(sorted(SUPPORTED_CODING_AGENTS))
        logger.error("Unsupported CODING_AGENT=%r (expected one of: %s)", agent, supported)
        sys.exit(1)
    return agent


def _agent_display_name(agent: str) -> str:
    mapping = {
        "opencode": "OpenCode",
        "aider": "Aider",
        "kilo": "Kilo Code",
    }
    return mapping.get(agent, agent)


def _agent_git_identity(agent: str) -> tuple[str, str]:
    if agent == "aider":
        return "Aider Bot", "aider-bot@localhost"
    if agent == "kilo":
        return "Kilo Bot", "kilo-bot@localhost"
    return "OpenCode Bot", "opencode-bot@localhost"


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
# Agent helpers
# ---------------------------------------------------------------------------


def _write_openai_compatible_config(
    config_path: Path,
    *,
    schema_url: str,
    base_url: str,
    model: str,
    api_key: str,
    max_context_tokens: int,
    max_output_tokens: int,
) -> None:
    provider_name = "custom"
    cfg = {
        "$schema": schema_url,
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
    _write_openai_compatible_config(
        config_path,
        schema_url="https://opencode.ai/config.json",
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_context_tokens=max_context_tokens,
        max_output_tokens=max_output_tokens,
    )


def _write_kilo_config(
    config_path: Path,
    *,
    base_url: str,
    model: str,
    api_key: str,
    max_context_tokens: int,
    max_output_tokens: int,
) -> None:
    """Write an OpenCode-compatible provider config consumed by Kilo CLI."""
    _write_openai_compatible_config(
        config_path,
        schema_url="https://kilo.ai/config.json",
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_context_tokens=max_context_tokens,
        max_output_tokens=max_output_tokens,
    )


def _write_aider_model_metadata(
    config_path: Path,
    *,
    model: str,
    max_context_tokens: int,
    max_output_tokens: int,
) -> None:
    """Write model metadata so Aider knows context/output limits for custom models."""
    model_name = model if "/" in model else f"openai/{model}"
    entry = {
        "max_tokens": max_output_tokens,
        "max_input_tokens": max_context_tokens,
        "max_output_tokens": max_output_tokens,
        "litellm_provider": "openai",
        "mode": "chat",
    }
    cfg: dict[str, dict[str, object]] = {model_name: dict(entry)}
    # Keep a plain-model alias for compatibility if model is provided unqualified.
    if model_name != model:
        cfg[model] = dict(entry)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote %s", config_path)


@dataclass(frozen=True)
class _AgentExecutionSettings:
    base_url: str
    api_key: str
    model: str
    config_path: Path
    data_dir: Path
    timeout_seconds: int


def _tail_lines(text: str, n: int = AGENT_STDIO_TAIL_LINES) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-n:])


class _BaseAgentExecutor(ABC):
    agent_key: str
    display_name: str
    binary_name: str

    def __init__(self, settings: _AgentExecutionSettings) -> None:
        self.settings = settings

    @abstractmethod
    def _build_command(self, *, cwd: Path, prompt: str) -> list[str]:
        pass

    def _common_env(self) -> dict[str, str]:
        env = os.environ.copy()
        home_dir = self.settings.data_dir / "home"
        xdg_cache = self.settings.data_dir / "xdg-cache"
        xdg_config = self.settings.data_dir / "xdg-config"
        xdg_data = self.settings.data_dir / "xdg-data"
        home_dir.mkdir(parents=True, exist_ok=True)
        xdg_cache.mkdir(parents=True, exist_ok=True)
        xdg_config.mkdir(parents=True, exist_ok=True)
        xdg_data.mkdir(parents=True, exist_ok=True)

        env["HOME"] = str(home_dir)
        env["XDG_CACHE_HOME"] = str(xdg_cache)
        env["XDG_CONFIG_HOME"] = str(xdg_config)
        env["XDG_DATA_HOME"] = str(xdg_data)
        env.setdefault("DEBIAN_FRONTEND", "noninteractive")
        return env

    def _prepare_env(self) -> dict[str, str]:
        return self._common_env()

    def _log_extra_context(self) -> None:
        pass

    def run(self, *, cwd: Path, prompt: str) -> str:
        cmd = self._build_command(cwd=cwd, prompt=prompt)
        display_cmd = [*cmd[:-1], "<prompt>"] if cmd else []
        logger.info("Running %s in %s", self.agent_key, cwd)
        logger.info("%s command: %s", self.display_name, " ".join(display_cmd))
        self._log_extra_context()

        env = self._prepare_env()

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
            raise RuntimeError(
                f"{self.binary_name} binary not found in runner image"
            ) from exc

        stdout_buf: list[str] = []
        stderr_buf: list[str] = []

        def _pump(stream: Optional[TextIO], prefix: str, buf: list[str]) -> None:
            if stream is None:
                return
            for line in iter(stream.readline, ""):
                buf.append(line)
                text = line.rstrip()
                if text:
                    logger.info("%s %s | %s", self.agent_key, prefix, text)
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
            result_code = proc.wait(timeout=self.settings.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raise RuntimeError(
                f"{self.agent_key} timed out after {self.settings.timeout_seconds}s"
            ) from exc
        finally:
            out_thread.join()
            err_thread.join()

        stdout = "".join(stdout_buf).strip()
        stderr = "".join(stderr_buf).strip()
        logger.info("%s exit code: %d", self.display_name, result_code)
        logger.info("%s stdout tail:\n%s", self.display_name, _tail_lines(stdout) or "<empty>")
        logger.info("%s stderr tail:\n%s", self.display_name, _tail_lines(stderr) or "<empty>")

        if result_code != 0:
            detail = stderr[-3000:] if stderr else stdout[-3000:]
            raise RuntimeError(
                f"{self.agent_key} failed with exit code {result_code}: {detail}"
            )
        if not stdout:
            raise RuntimeError(f"{self.agent_key} returned an empty response")
        return stdout


class _OpenCodeExecutor(_BaseAgentExecutor):
    agent_key = "opencode"
    display_name = "OpenCode"
    binary_name = "opencode"

    def _build_command(self, *, cwd: Path, prompt: str) -> list[str]:
        return ["opencode", "run", "--model", f"custom/{self.settings.model}", prompt]

    def _prepare_env(self) -> dict[str, str]:
        env = super()._prepare_env()
        env["OPENCODE_CONFIG"] = str(self.settings.config_path)
        return env


class _AiderExecutor(_BaseAgentExecutor):
    agent_key = "aider"
    display_name = "Aider"
    binary_name = "aider"

    def _build_command(self, *, cwd: Path, prompt: str) -> list[str]:
        model = self.settings.model
        if not model.startswith("openai/"):
            model = f"openai/{model}"
        chat_history_file = self.settings.data_dir / ".aider.chat.history.md"
        input_history_file = self.settings.data_dir / ".aider.input.history"
        # Headless mode: --message runs one request and exits.
        return [
            "aider",
            "--model",
            model,
            "--yes",
            "--no-fancy-input",
            "--no-show-model-warnings",
            "--no-check-update",
            "--no-auto-commits",
            "--model-metadata-file",
            str(self.settings.config_path),
            "--chat-history-file",
            str(chat_history_file),
            "--input-history-file",
            str(input_history_file),
            "--message",
            prompt,
        ]

    def _prepare_env(self) -> dict[str, str]:
        env = super()._prepare_env()
        # Remove inherited Aider-specific settings from parent env so this
        # invocation is fully controlled by runner-provided values.
        for key in [k for k in env if k.startswith("AIDER_")]:
            env.pop(key, None)
        env["OPENAI_API_BASE"] = self.settings.base_url
        env["OPENAI_API_KEY"] = self.settings.api_key
        # Aider accepts provider-formatted credentials via AIDER_API_KEY.
        # Keep it explicit to avoid "Invalid --api-key format" failures.
        env["AIDER_API_KEY"] = f"openai={self.settings.api_key}"
        return env


class _KiloExecutor(_BaseAgentExecutor):
    agent_key = "kilo"
    display_name = "Kilo Code"
    binary_name = "kilo"

    def _build_command(self, *, cwd: Path, prompt: str) -> list[str]:
        return ["kilo", "run", "--auto", prompt]

    def _log_extra_context(self) -> None:
        logger.info("Kilo model configured as custom/%s", self.settings.model)

    def _prepare_env(self) -> dict[str, str]:
        env = super()._prepare_env()
        config_text = self.settings.config_path.read_text(encoding="utf-8")
        xdg_config = Path(env["XDG_CONFIG_HOME"])
        home_dir = Path(env["HOME"])
        config_targets = [
            xdg_config / "kilo" / "opencode.json",
            xdg_config / "kilocode" / "opencode.json",
            home_dir / ".config" / "kilo" / "opencode.json",
            home_dir / ".config" / "kilocode" / "opencode.json",
        ]
        for target in config_targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(config_text, encoding="utf-8")
        logger.info("Wrote Kilo config to %s", config_targets[0])
        return env


def _build_agent_executor(
    *,
    coding_agent: str,
    model: str,
    base_url: str,
    api_key: str,
    config_path: Path,
    data_dir: Path,
    timeout_seconds: int,
) -> _BaseAgentExecutor:
    settings = _AgentExecutionSettings(
        base_url=base_url,
        api_key=api_key,
        model=model,
        config_path=config_path,
        data_dir=data_dir,
        timeout_seconds=timeout_seconds,
    )
    if coding_agent == "aider":
        return _AiderExecutor(settings)
    if coding_agent == "kilo":
        return _KiloExecutor(settings)
    return _OpenCodeExecutor(settings)


def _run_agent(
    *,
    coding_agent: str,
    cwd: Path,
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    config_path: Path,
    data_dir: Path,
    timeout_seconds: int,
) -> str:
    executor = _build_agent_executor(
        coding_agent=coding_agent,
        model=model,
        base_url=base_url,
        api_key=api_key,
        config_path=config_path,
        data_dir=data_dir,
        timeout_seconds=timeout_seconds,
    )
    return executor.run(cwd=cwd, prompt=prompt)


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
    coding_agent: str,
    agent_user_prompt: str,
    agent_model: str,
    agent_base_url: str,
    agent_api_key: str,
    agent_config_path: Path,
    agent_data_dir: Path,
    agent_timeout_seconds: int,
    agent_workdir: Path,
) -> None:
    """Fetch MR diff, produce a review via selected agent, post as a note."""
    agent_name = _agent_display_name(coding_agent)
    logger.info("Starting REVIEW task for MR !%d with %s", mr_iid, coding_agent)

    mr = gl.get_mr(project_id, mr_iid)
    changes = gl.get_mr_changes(project_id, mr_iid)
    mr_notes = gl.get_mr_notes(project_id, mr_iid)

    title = mr.get("title", "")
    description = mr.get("description", "")
    diff_text = _format_diff(changes)
    notes_context = _format_notes_context(mr_notes)
    user_prompt = agent_user_prompt or "(none)"

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
        review_text = _run_agent(
            coding_agent=coding_agent,
            cwd=agent_workdir,
            prompt=prompt,
            model=agent_model,
            base_url=agent_base_url,
            api_key=agent_api_key,
            config_path=agent_config_path,
            data_dir=agent_data_dir,
            timeout_seconds=agent_timeout_seconds,
        )
    except RuntimeError as exc:
        logger.error("%s review failed: %s", agent_name, exc)
        gl.post_mr_note(
            project_id,
            mr_iid,
            f"⚠️ **{agent_name}**: review failed.\n\n```\n{exc}\n```",
        )
        sys.exit(1)

    note_body = f"## 🤖 {agent_name} Review\n\n{review_text}"
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
    coding_agent: str,
    agent_user_prompt: str,
    agent_model: str,
    agent_base_url: str,
    agent_api_key: str,
    agent_config_path: Path,
    agent_data_dir: Path,
    agent_timeout_seconds: int,
    precreated_mr_iid: Optional[int] = None,
    precreated_mr_url: str = "",
    precreated_mr_branch: str = "",
    precreated_mr_target_branch: str = "",
) -> None:
    """Fix an issue or MR: let selected coding agent edit code, push branch, open MR."""
    agent_name = _agent_display_name(coding_agent)
    git_user_name, git_user_email = _agent_git_identity(coding_agent)
    logger.info(
        "Starting FIX task (%s) for %s #%d with %s",
        task_kind,
        kind,
        iid,
        coding_agent,
    )

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
            f"This MR was automatically generated by {agent_name} in response to "
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
            f"This MR was automatically generated by {agent_name} in response to "
            f"[MR !{iid}]({item.get('web_url', '')})."
        )

    ws.clone(
        gitlab_base_url=gitlab_base_url,
        path_with_namespace=path_with_namespace,
        token=gitlab_token,
        branch=base_branch,
        git_user_name=git_user_name,
        git_user_email=git_user_email,
    )
    if use_precreated_issue_mr:
        ws.checkout_remote_branch(new_branch)
    else:
        ws.create_branch(new_branch)

    notes_context = _format_notes_context(item_notes)
    user_prompt = agent_user_prompt or "(none)"
    prompt = textwrap.dedent(
        f"""\
        You are operating inside a Linux container with a git repository.

        <critical_rules>
These rules override everything else. Follow them strictly:

1. **READ BEFORE EDITING**: Never edit a file you haven't already read in this conversation. Once read, you don't need to re-read unless it changed. Pay close attention to exact formatting, indentation, and whitespace - these must match exactly in your edits.
2. **BE AUTONOMOUS**: Don't ask questions - search, read, think, decide, act. Break complex tasks into steps and complete them all. Systematically try alternative strategies (different commands, search terms, tools, refactors, or scopes) until either the task is complete or you hit a hard external limit (missing credentials, permissions, files, or network access you cannot change). Only stop for actual blocking errors, not perceived difficulty.
3. **TEST AFTER CHANGES**: Run tests immediately after each modification.
4. **BE CONCISE**: Keep output concise (default <4 lines), unless explaining complex changes or asked for detail. Conciseness applies to output only, not to thoroughness of work.
5. **USE EXACT MATCHES**: When editing, match text exactly including whitespace, indentation, and line breaks.
6. **NEVER COMMIT**: Unless user explicitly says "commit".
7. **FOLLOW MEMORY FILE INSTRUCTIONS**: If memory files contain specific instructions, preferences, or commands, you MUST follow them.
8. **NEVER ADD COMMENTS**: Only add comments if the user asked you to do so. Focus on *why* not *what*. NEVER communicate with the user through code comments.
9. **SECURITY FIRST**: Only assist with defensive security tasks. Refuse to create, modify, or improve code that may be used maliciously.
10. **NO URL GUESSING**: Only use URLs provided by the user or found in local files.
11. **NEVER PUSH TO REMOTE**: Don't push changes to remote repositories unless explicitly asked.
12. **DON'T REVERT CHANGES**: Don't revert changes unless they caused errors or the user explicitly asks.
13. **TOOL CONSTRAINTS**: Only use documented tools. Never attempt 'apply_patch' or 'apply_diff' - they don't exist. Use 'edit' or 'multiedit' instead.
</critical_rules>

<communication_style>
Keep responses minimal:
- Under 4 lines of text (tool use doesn't count)
- Conciseness is about **text only**: always fully implement the requested feature, tests, and wiring even if that requires many tool calls.
- No preamble ("Here's...", "I'll...")
- No postamble ("Let me know...", "Hope this helps...")
- One-word answers when possible
- No emojis ever
- No explanations unless user asks
- Never send acknowledgement-only responses; after receiving new context or instructions, immediately continue the task or state the concrete next action you will take.
- Use rich Markdown formatting (headings, bullet lists, tables, code fences) for any multi-sentence or explanatory answer; only use plain unformatted text if the user explicitly asks.

Examples:
user: what is 2+2?
assistant: 4

user: list files in src/
assistant: [uses ls tool]
foo.c, bar.c, baz.c

user: which file has the foo implementation?
assistant: src/foo.c

user: add error handling to the login function
assistant: [searches for login, reads file, edits with exact match, runs tests]
Done

user: Where are errors from the client handled?
assistant: Clients are marked as failed in the `connectToServer` function in src/services/process.go:712.
</communication_style>

<code_references>
When referencing specific functions or code locations, use the pattern `file_path:line_number` to help users navigate:
- Example: "The error is handled in src/main.go:45"
- Example: "See the implementation in pkg/utils/helper.go:123-145"
</code_references>

<workflow>
For every task, follow this sequence internally (don't narrate it):

**Before acting**:
- Search codebase for relevant files
- Read files to understand current state
- Check memory for stored commands
- Identify what needs to change
- Use `git log` and `git blame` for additional context when needed

**While acting**:
- Read entire file before editing it
- Before editing: verify exact whitespace and indentation from View output
- Use exact text for find/replace (include whitespace)
- Make one logical change at a time
- After each change: run tests
- If tests fail: fix immediately
- If edit fails: read more context, don't guess - the text must match exactly
- Keep going until query is completely resolved before yielding to user
- For longer tasks, send brief progress updates (under 10 words) BUT IMMEDIATELY CONTINUE WORKING - progress updates are not stopping points

**Before finishing**:
- Verify ENTIRE query is resolved (not just first step)
- All described next steps must be completed
- Cross-check the original prompt and your own mental checklist; if any feasible part remains undone, continue working instead of responding.
- Run lint/typecheck if in memory
- Verify all changes work
- Keep response under 4 lines

**Key behaviors**:
- Use find_references before changing shared code
- Follow existing patterns (check similar files)
- If stuck, try different approach (don't repeat failures)
- Make decisions yourself (search first, don't ask)
- Fix problems at root cause, not surface-level patches
- Don't fix unrelated bugs or broken tests (mention them in final message if relevant)
</workflow>

<decision_making>
**Make decisions autonomously** - don't ask when you can:
- Search to find the answer
- Read files to see patterns
- Check similar code
- Infer from context
- Try most likely approach
- When requirements are underspecified but not obviously dangerous, make the most reasonable assumptions based on project patterns and memory files, briefly state them if needed, and proceed instead of waiting for clarification.

**Only stop/ask user if**:
- Truly ambiguous business requirement
- Multiple valid approaches with big tradeoffs
- Could cause data loss
- Exhausted all attempts and hit actual blocking errors

**When requesting information/access**:
- Exhaust all available tools, searches, and reasonable assumptions first.
- Never say "Need more info" without detail.
- In the same message, list each missing item, why it is required, acceptable substitutes, and what you already attempted.
- State exactly what you will do once the information arrives so the user knows the next step.

When you must stop, first finish all unblocked parts of the request, then clearly report: (a) what you tried, (b) exactly why you are blocked, and (c) the minimal external action required. Don't stop just because one path failed—exhaust multiple plausible approaches first.

**Never stop for**:
- Task seems too large (break it down)
- Multiple files to change (change them)
- Concerns about "session limits" (no such limits exist)
- Work will take many steps (do all the steps)

Examples of autonomous decisions:
- File location → search for similar files
- Test command → check package.json/memory
- Code style → read existing code
- Library choice → check what's used
- Naming → follow existing names
</decision_making>

<editing_files>

Critical: ALWAYS read files before editing them in this conversation.

When using edit tools:
1. Read the file first - note the EXACT indentation (spaces vs tabs, count)
2. Copy the exact text including ALL whitespace, newlines, and indentation
3. Include 3-5 lines of context before and after the target
4. Verify your old_string would appear exactly once in the file
5. If uncertain about whitespace, include more surrounding context
6. Verify edit succeeded
7. Run tests

**Whitespace matters**:
- Count spaces/tabs carefully (use View tool line numbers as reference)
- Include blank lines if they exist
- Match line endings exactly
- When in doubt, include MORE context rather than less

Efficiency tips:
- Don't re-read files after successful edits (tool will fail if it didn't work)
- Same applies for making folders, deleting files, etc.

Common mistakes to avoid:
- Editing without reading first
- Approximate text matches
- Wrong indentation (spaces vs tabs, wrong count)
- Missing or extra blank lines
- Not enough context (text appears multiple times)
- Trimming whitespace that exists in the original
- Not testing after changes
</editing_files>

<whitespace_and_exact_matching>
The Edit tool is extremely literal. "Close enough" will fail.

**Before every edit**:
1. View the file and locate the exact lines to change
2. Copy the text EXACTLY including:
   - Every space and tab
   - Every blank line
   - Opening/closing braces position
   - Comment formatting
3. Include enough surrounding lines (3-5) to make it unique
4. Double-check indentation level matches

**Common failures**:
- `func foo() {{` vs `func foo(){{` (space before brace)
- Tab vs 4 spaces vs 2 spaces
- Missing blank line before/after
- `// comment` vs `//comment` (space after //)
- Different number of spaces in indentation

**If edit fails**:
- View the file again at the specific location
- Copy even more context
- Check for tabs vs spaces
- Verify line endings
- Try including the entire function/block if needed
- Never retry with guessed changes - get the exact text first
</whitespace_and_exact_matching>

<task_completion>
Ensure every task is implemented completely, not partially or sketched.

1. **Think before acting** (for non-trivial tasks)
   - Identify all components that need changes (models, logic, routes, config, tests, docs)
   - Consider edge cases and error paths upfront
   - Form a mental checklist of requirements before making the first edit
   - This planning happens internally - don't narrate it to the user

2. **Implement end-to-end**
   - Treat every request as complete work: if adding a feature, wire it fully
   - Update all affected files (callers, configs, tests, docs)
   - Don't leave TODOs or "you'll also need to..." - do it yourself
   - No task is too large - break it down and complete all parts
   - For multi-part prompts, treat each bullet/question as a checklist item and ensure every item is implemented or answered. Partial completion is not an acceptable final state.

3. **Verify before finishing**
   - Re-read the original request and verify each requirement is met
   - Check for missing error handling, edge cases, or unwired code
   - Run tests to confirm the implementation works
   - Only say "Done" when truly done - never stop mid-task
</task_completion>

<error_handling>
When errors occur:
1. Read complete error message
2. Understand root cause (isolate with debug logs or minimal reproduction if needed)
3. Try different approach (don't repeat same action)
4. Search for similar code that works
5. Make targeted fix
6. Test to verify
7. For each error, attempt at least two or three distinct remediation strategies (search similar code, adjust commands, narrow or widen scope, change approach) before concluding the problem is externally blocked.

Common errors:
- Import/Module → check paths, spelling, what exists
- Syntax → check brackets, indentation, typos
- Tests fail → read test, see what it expects
- File not found → use ls, check exact path

**Edit tool "old_string not found"**:
- View the file again at the target location
- Copy the EXACT text including all whitespace
- Include more surrounding context (full function if needed)
- Check for tabs vs spaces, extra/missing blank lines
- Count indentation spaces carefully
- Don't retry with approximate matches - get the exact text
</error_handling>

<memory_instructions>
Memory files store commands, preferences, and codebase info. Update them when you discover:
- Build/test/lint commands
- Code style preferences  
- Important codebase patterns
- Useful project information
</memory_instructions>

<code_conventions>
Before writing code:
1. Check if library exists (look at imports, package.json)
2. Read similar code for patterns
3. Match existing style
4. Use same libraries/frameworks
5. Follow security best practices (never log secrets)
6. Don't use one-letter variable names unless requested

Never assume libraries are available - verify first.

**Ambition vs. precision**:
- New projects → be creative and ambitious with implementation
- Existing codebases → be surgical and precise, respect surrounding code
- Don't change filenames or variables unnecessarily
- Don't add formatters/linters/tests to codebases that don't have them
</code_conventions>

<testing>
After significant changes:
- Start testing as specific as possible to code changed, then broaden to build confidence
- Use self-verification: write unit tests, add output logs, or use debug statements to verify your solutions
- Run relevant test suite
- If tests fail, fix before continuing
- Check memory for test commands
- Run lint/typecheck if available (on precise targets when possible)
- For formatters: iterate max 3 times to get it right; if still failing, present correct solution and note formatting issue
- Suggest adding commands to memory if not found
- Don't fix unrelated bugs or test failures (not your responsibility)
</testing>

<tool_usage>
- Search before assuming
- Read files before editing
- Always use absolute paths for file operations (editing, reading, writing)
- Run tools in parallel when safe (no dependencies)
- When making multiple independent bash calls, send them in a single message with multiple tool calls for parallel execution
- Summarize tool output for user (they don't see it)
- Only use the tools you know exist.

<bash_commands>

When running non-trivial bash commands (especially those that modify the system):
- Briefly explain what the command does and why you're running it
- This ensures the user understands potentially dangerous operations
- Simple read-only commands (ls, cat, etc.) don't need explanation
- Use `&` for background processes that won't stop on their own (e.g., `node server.js &`)
- Avoid interactive commands - use non-interactive versions (e.g., `npm init -y` not `npm init`)
- Combine related commands to save time (e.g., `git status && git diff HEAD && git log -n 3`)
</bash_commands>
</tool_usage>

<proactiveness>
Balance autonomy with user intent:
- When asked to do something → do it fully (including ALL follow-ups and "next steps")
- Never describe what you'll do next - just do it
- When the user provides new information or clarification, incorporate it immediately and keep executing instead of stopping with an acknowledgement.
- Responding with only a plan, outline, or TODO list (or any other purely verbal response) is failure; you must execute the plan via tools whenever execution is possible.
- When asked how to approach → explain first, don't auto-implement
- After completing work → stop, don't explain (unless asked)
- Don't surprise user with unexpected actions
</proactiveness>

<final_answers>
Adapt verbosity to match the work completed:

**Default (under 4 lines)**:
- Simple questions or single-file changes
- Casual conversation, greetings, acknowledgements
- One-word answers when possible

**More detail allowed (up to 10-15 lines)**:
- Large multi-file changes that need walkthrough
- Complex refactoring where rationale adds value
- Tasks where understanding the approach is important
- When mentioning unrelated bugs/issues found
- Suggesting logical next steps user might want
- Structure longer answers with Markdown sections and lists, and put all code, commands, and config in fenced code blocks.

**What to include in verbose answers**:
- Brief summary of what was done and why
- Key files/functions changed (with `file:line` references)
- Any important decisions or tradeoffs made
- Next steps or things user should verify
- Issues found but not fixed

**What to avoid**:
- Don't show full file contents unless explicitly asked
- Don't explain how to save files or copy code (user has access to your work)
- Don't use "Here's what I did" or "Let me know if..." style preambles/postambles
- Keep tone direct and factual, like handing off work to a teammate
</final_answers>

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
        agent_summary = _run_agent(
            coding_agent=coding_agent,
            cwd=ws.repo_dir,
            prompt=prompt,
            model=agent_model,
            base_url=agent_base_url,
            api_key=agent_api_key,
            config_path=agent_config_path,
            data_dir=agent_data_dir,
            timeout_seconds=agent_timeout_seconds,
        )
    except RuntimeError as exc:
        logger.error("%s fix failed: %s", agent_name, exc)
        _log_post_agent_git_diagnostics(ws.repo_dir)
        gl.post_note(
            project_id,
            kind,
            iid,
            f"⚠️ **{agent_name}**: automated fix failed.\n\n```\n{exc}\n```",
        )
        sys.exit(1)

    _log_post_agent_git_diagnostics(ws.repo_dir)

    if not ws.has_changes():
        msg = "No filesystem changes detected."
        logger.error(msg)
        gl.post_note(project_id, kind, iid, f"⚠️ **{agent_name}**: {msg}")
        sys.exit(1)

    commit_msg = f"chore: {agent_name} automated fix for {back_ref}\n\nTask: {item_title}"
    ws.commit_all(commit_msg)

    if not ws.has_changes() and not _branch_has_commits(ws, base_branch, new_branch):
        msg = (
            "No filesystem changes detected and no commits were produced."
        )
        logger.error(msg)
        gl.post_note(project_id, kind, iid, f"⚠️ **{agent_name}**: {msg}")
        sys.exit(1)

    passed, test_output = ws.run_tests()
    if not passed:
        test_snippet = test_output[-3000:] if len(test_output) > 3000 else test_output
        gl.post_note(
            project_id,
            kind,
            iid,
            f"⚠️ **{agent_name}**: tests failed after applying changes. "
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
            f"🤖 **{agent_name}** updated merge request {mr_ref}.\n\n"
            f"Branch: `{new_branch}`",
        )
        if precreated_mr_iid is not None:
            gl.post_mr_note(
                project_id,
                precreated_mr_iid,
                f"🤖 **{agent_name}** updated this merge request from the linked issue.\n\n"
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
            f"⚠️ **{agent_name}**: branch `{new_branch}` was pushed but MR creation failed.\n"
            f"Please open the MR manually.\n\n```\n{exc}\n```",
        )
        sys.exit(1)

    summary_tail = agent_summary[-2000:] if len(agent_summary) > 2000 else agent_summary
    gl.post_note(
        project_id,
        kind,
        iid,
        f"🤖 **{agent_name}** created a fix in !{new_mr_iid}: {new_mr_url}\n\n"
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
    coding_agent = _parse_coding_agent(
        _optional("CODING_AGENT", _optional("DEFAULT_CODING_AGENT", DEFAULT_CODING_AGENT))
    )
    agent_name = _agent_display_name(coding_agent)
    agent_user_prompt = _optional(
        "AGENT_USER_PROMPT",
        _optional("OPENCODE_USER_PROMPT", _optional("AIDER_USER_PROMPT")),
    )

    agent_base_url = _require_any(
        "LLM_BASE_URL", "OPENCODE_BASE_URL", "AIDER_BASE_URL", "KILO_BASE_URL"
    )
    agent_model = _require_any("LLM_MODEL", "OPENCODE_MODEL", "AIDER_MODEL", "KILO_MODEL")
    agent_api_key = _require_any(
        "LLM_API_KEY", "OPENCODE_API_KEY", "AIDER_API_KEY", "KILO_API_KEY"
    )
    agent_timeout_seconds = _parse_int_env_any(
        (
            "LLM_TIMEOUT_SECONDS",
            "OPENCODE_TIMEOUT_SECONDS",
            "AIDER_TIMEOUT_SECONDS",
            "KILO_TIMEOUT_SECONDS",
        ),
        DEFAULT_AGENT_TIMEOUT_SECONDS,
    )
    agent_max_context_tokens = _parse_int_env_any(
        (
            "LLM_MAX_CONTEXT_TOKENS",
            "OPENCODE_MAX_CONTEXT_TOKENS",
            "AIDER_MAX_CONTEXT_TOKENS",
            "KILO_MAX_CONTEXT_TOKENS",
        ),
        DEFAULT_AGENT_MAX_CONTEXT_TOKENS,
    )
    if agent_max_context_tokens < 1:
        logger.warning(
            "Invalid max context tokens=%d, using default %d",
            agent_max_context_tokens,
            DEFAULT_AGENT_MAX_CONTEXT_TOKENS,
        )
        agent_max_context_tokens = DEFAULT_AGENT_MAX_CONTEXT_TOKENS
    agent_max_output_tokens = _parse_int_env_any(
        (
            "LLM_MAX_OUTPUT_TOKENS",
            "OPENCODE_MAX_OUTPUT_TOKENS",
            "AIDER_MAX_OUTPUT_TOKENS",
            "KILO_MAX_OUTPUT_TOKENS",
        ),
        DEFAULT_AGENT_MAX_OUTPUT_TOKENS,
    )
    if agent_max_output_tokens < 1:
        logger.warning(
            "Invalid max output tokens=%d, using default %d",
            agent_max_output_tokens,
            DEFAULT_AGENT_MAX_OUTPUT_TOKENS,
        )
        agent_max_output_tokens = DEFAULT_AGENT_MAX_OUTPUT_TOKENS

    logger.info(
        "Configured coding agent: %s (model=%s, timeout=%ss)",
        agent_name,
        agent_model,
        agent_timeout_seconds,
    )
    logger.info("Shared LLM endpoint (all agents): %s", agent_base_url)

    gl = GitLabClient(
        base_url=_require("GITLAB_BASE_URL"),
        token=_require("GITLAB_TOKEN"),
    )

    workspace_root = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
    workspace_root.mkdir(parents=True, exist_ok=True)

    agent_data_dir = workspace_root / f".{coding_agent}-runner-data"
    agent_data_dir.mkdir(parents=True, exist_ok=True)
    if coding_agent == "kilo":
        agent_config_path = workspace_root / ".kilo-runner-config" / "opencode.json"
        _write_kilo_config(
            agent_config_path,
            base_url=agent_base_url,
            model=agent_model,
            api_key=agent_api_key,
            max_context_tokens=agent_max_context_tokens,
            max_output_tokens=agent_max_output_tokens,
        )
    elif coding_agent == "aider":
        agent_config_path = workspace_root / ".aider-runner-config" / "model-metadata.json"
        _write_aider_model_metadata(
            agent_config_path,
            model=agent_model,
            max_context_tokens=agent_max_context_tokens,
            max_output_tokens=agent_max_output_tokens,
        )
    else:
        agent_config_path = workspace_root / ".opencode-runner-config" / "opencode.json"
        _write_opencode_config(
            agent_config_path,
            base_url=agent_base_url,
            model=agent_model,
            api_key=agent_api_key,
            max_context_tokens=agent_max_context_tokens,
            max_output_tokens=agent_max_output_tokens,
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
            coding_agent=coding_agent,
            agent_user_prompt=agent_user_prompt,
            agent_model=agent_model,
            agent_base_url=agent_base_url,
            agent_api_key=agent_api_key,
            agent_config_path=agent_config_path,
            agent_data_dir=agent_data_dir,
            agent_timeout_seconds=agent_timeout_seconds,
            agent_workdir=workspace_root,
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
            coding_agent=coding_agent,
            agent_user_prompt=agent_user_prompt,
            agent_model=agent_model,
            agent_base_url=agent_base_url,
            agent_api_key=agent_api_key,
            agent_config_path=agent_config_path,
            agent_data_dir=agent_data_dir,
            agent_timeout_seconds=agent_timeout_seconds,
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
