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
SUPPORTED_CODING_AGENTS = {"opencode", "crush", "kilo"}
DEFAULT_AGENT_TIMEOUT_SECONDS = 1800
DEFAULT_AGENT_MAX_CONTEXT_TOKENS = 128000
DEFAULT_AGENT_MAX_OUTPUT_TOKENS = 100000
DEFAULT_CRUSH_ALLOWED_TOOLS = "view,ls,grep,edit,bash"
DEFAULT_CRUSH_MAX_TOKENS = 4096
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
        "crush": "Crush",
        "kilo": "Kilo Code",
    }
    return mapping.get(agent, agent)


def _agent_git_identity(agent: str) -> tuple[str, str]:
    if agent == "crush":
        return "Crush Bot", "crush-bot@localhost"
    if agent == "kilo":
        return "Kilo Bot", "kilo-bot@localhost"
    return "OpenCode Bot", "opencode-bot@localhost"


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


@dataclass(frozen=True)
class _AgentExecutionSettings:
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


class _CrushExecutor(_BaseAgentExecutor):
    agent_key = "crush"
    display_name = "Crush"
    binary_name = "crush"

    def _build_command(self, *, cwd: Path, prompt: str) -> list[str]:
        return [
            "crush",
            "-y",
            "-c",
            str(cwd),
            "-D",
            str(self.settings.data_dir),
            "run",
            prompt,
        ]

    def _prepare_env(self) -> dict[str, str]:
        env = super()._prepare_env()
        env["CRUSH_DISABLE_METRICS"] = "1"
        # CRUSH_GLOBAL_CONFIG expects a directory; crush appends "/crush.json".
        env["CRUSH_GLOBAL_CONFIG"] = str(self.settings.config_path.parent)
        env["CRUSH_GLOBAL_DATA"] = str(self.settings.data_dir)
        env.setdefault("CRUSH_DISABLE_PROVIDER_AUTO_UPDATE", "1")
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
    config_path: Path,
    data_dir: Path,
    timeout_seconds: int,
) -> _BaseAgentExecutor:
    settings = _AgentExecutionSettings(
        model=model,
        config_path=config_path,
        data_dir=data_dir,
        timeout_seconds=timeout_seconds,
    )
    if coding_agent == "crush":
        return _CrushExecutor(settings)
    if coding_agent == "kilo":
        return _KiloExecutor(settings)
    return _OpenCodeExecutor(settings)


def _run_agent(
    *,
    coding_agent: str,
    cwd: Path,
    prompt: str,
    model: str,
    config_path: Path,
    data_dir: Path,
    timeout_seconds: int,
) -> str:
    executor = _build_agent_executor(
        coding_agent=coding_agent,
        model=model,
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

        STEP 3: UPDATE .gitignore (MANDATORY FOR NEW PROJECTS/DIRECTORIES)
        -------------------------------------------------------------------
           When creating new project directories or adding new tooling, ALWAYS update .gitignore:
           
           Node.js/Next.js projects - Add to .gitignore:
           → node_modules/
           → .next/
           → out/
           → dist/
           → build/
           → .env*.local
           → *.log
           
           Python projects - Add to .gitignore:
           → __pycache__/
           → *.pyc
           → *.pyo
           → *.pyd
           → .Python
           → venv/
           → 5nv/
           → .venv/
           → *.egg-info/
           → dist/
           → build/
           
           Go projects - Add to .gitignore:
           → /bin/
           → /pkg/
           → *.exe
           → *.test
           
           Rust projects - Add to .gitignore:
           → /target/
           → Cargo.lock (for libraries)
           
           General - Add to .gitignore:
           → .DS_Store
           → .idea/
           → .vscode/ (unless project-specific settings)
           → *.swp
           → *.swo
           
           ⚠️ IMPORTANT:
           - Create .gitignore if it doesn't exist
           - Append entries if .gitignore already exists (don't overwrite)
           - Check if entries already exist before adding duplicates
           - Place .gitignore in the appropriate directory (project root or subdirectory)

        STEP 4: INSTALL DEPENDENCIES (ABSOLUTELY MANDATORY - NO EXCEPTIONS)
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

        STEP 6: FIX ALL ERRORS - ITERATE UNTIL SUCCESSFUL
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

        STEP 7: REPORT VALIDATION SUCCESS
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
        
        ✗ Skipping .gitignore updates for new projects
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
        
        BeUpdated .gitignore with necessary exclusions (node_modules/, __pycache__/, etc.)
        □ fore you finish, verify you have:
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
        agent_summary = _run_agent(
            coding_agent=coding_agent,
            cwd=ws.repo_dir,
            prompt=prompt,
            model=agent_model,
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
        _optional("OPENCODE_USER_PROMPT", _optional("CRUSH_USER_PROMPT")),
    )

    agent_base_url = _require_any(
        "LLM_BASE_URL", "OPENCODE_BASE_URL", "CRUSH_BASE_URL", "KILO_BASE_URL"
    )
    agent_model = _require_any("LLM_MODEL", "OPENCODE_MODEL", "CRUSH_MODEL", "KILO_MODEL")
    agent_api_key = _require_any(
        "LLM_API_KEY", "OPENCODE_API_KEY", "CRUSH_API_KEY", "KILO_API_KEY"
    )
    agent_timeout_seconds = _parse_int_env_any(
        (
            "LLM_TIMEOUT_SECONDS",
            "OPENCODE_TIMEOUT_SECONDS",
            "CRUSH_TIMEOUT_SECONDS",
            "KILO_TIMEOUT_SECONDS",
        ),
        DEFAULT_AGENT_TIMEOUT_SECONDS,
    )
    agent_max_context_tokens = _parse_int_env_any(
        ("LLM_MAX_CONTEXT_TOKENS", "OPENCODE_MAX_CONTEXT_TOKENS", "KILO_MAX_CONTEXT_TOKENS"),
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
    crush_max_tokens = _parse_int_env_any(("CRUSH_MAX_TOKENS",), DEFAULT_CRUSH_MAX_TOKENS)
    if crush_max_tokens < 1:
        logger.warning(
            "Invalid CRUSH_MAX_TOKENS=%d, using default %d",
            crush_max_tokens,
            DEFAULT_CRUSH_MAX_TOKENS,
        )
        crush_max_tokens = DEFAULT_CRUSH_MAX_TOKENS
    crush_allowed_tools = _parse_allowed_tools(
        _optional("CRUSH_ALLOWED_TOOLS", DEFAULT_CRUSH_ALLOWED_TOOLS)
    )

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
    if coding_agent == "crush":
        agent_config_path = workspace_root / ".crush-runner-config" / "crush.json"
        _write_crush_config(
            agent_config_path,
            base_url=agent_base_url,
            model=agent_model,
            api_key=agent_api_key,
            allowed_tools=crush_allowed_tools,
            max_tokens=crush_max_tokens,
        )
    elif coding_agent == "kilo":
        agent_config_path = workspace_root / ".kilo-runner-config" / "opencode.json"
        _write_kilo_config(
            agent_config_path,
            base_url=agent_base_url,
            model=agent_model,
            api_key=agent_api_key,
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
