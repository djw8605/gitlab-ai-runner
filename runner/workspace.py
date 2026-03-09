"""Workspace helper: clone repo, create branch, commit, push, run tests."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Maximum number of characters fed to the review prompt for diffs/descriptions.
MAX_DIFF_CHARS = 24_000


def _run(
    cmd: list[str],
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    """Run a shell command, masking secrets from the log output."""
    safe_cmd = _mask_cmd(cmd)
    logger.info("$ %s", " ".join(safe_cmd))
    merged_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=merged_env,
        capture_output=capture,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        logger.error("Command failed (exit %d):\n%s\n%s", result.returncode, stdout, stderr)
        if check:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, output=stdout, stderr=stderr
            )
    return result


def _mask_cmd(cmd: list[str]) -> list[str]:
    """Replace tokens that look like secrets in command list."""
    token = os.environ.get("GITLAB_TOKEN", "")
    masked = []
    for part in cmd:
        if token and token in part:
            part = part.replace(token, "****")
        masked.append(part)
    return masked


def _slugify(text: str, max_len: int = 30) -> str:
    """Convert text to a lowercase slug."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:max_len].rstrip("-")


class Workspace:
    """Manages a git workspace inside the runner Job."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._repo_dir: Optional[Path] = None

    @property
    def repo_dir(self) -> Path:
        if self._repo_dir is None:
            raise RuntimeError("Repository not yet cloned")
        return self._repo_dir

    # ------------------------------------------------------------------
    # Clone / branch / commit / push
    # ------------------------------------------------------------------

    def clone(
        self,
        gitlab_base_url: str,
        path_with_namespace: str,
        token: str,
        branch: str = "main",
    ) -> Path:
        """Clone the repo into self.root/<project_name>.

        Uses HTTPS with oauth2 token credential.
        """
        parsed = urlparse(gitlab_base_url)
        clone_url = (
            f"{parsed.scheme}://oauth2:{token}@{parsed.netloc}"
            f"/{path_with_namespace}.git"
        )

        project_name = path_with_namespace.split("/")[-1]
        dest = self.root / project_name
        if dest.exists():
            logger.info("Destination %s already exists – skipping clone", dest)
            self._repo_dir = dest
            return dest

        logger.info("Cloning %s (branch=%s) …", path_with_namespace, branch)
        _run(
            ["git", "clone", "--depth=50", "--branch", branch, clone_url, str(dest)],
            env={"GIT_TERMINAL_PROMPT": "0"},
        )
        self._repo_dir = dest

        # Configure git identity for commits
        _run(["git", "config", "user.email", "opencode-bot@localhost"], cwd=dest)
        _run(["git", "config", "user.name", "OpenCode Bot"], cwd=dest)

        return dest

    def create_branch(self, branch_name: str) -> None:
        """Create and checkout a new branch."""
        _run(["git", "checkout", "-b", branch_name], cwd=self.repo_dir)

    def checkout_remote_branch(self, branch_name: str) -> None:
        """Checkout an existing branch from origin."""
        # Clone uses --branch/--single-branch, so plain `git fetch origin <branch>`
        # may only populate FETCH_HEAD and not refs/remotes/origin/<branch>.
        # Fetch with an explicit refspec so the remote-tracking ref exists.
        _run(
            [
                "git",
                "fetch",
                "origin",
                f"refs/heads/{branch_name}:refs/remotes/origin/{branch_name}",
            ],
            cwd=self.repo_dir,
        )
        _run(
            ["git", "checkout", "-B", branch_name, f"origin/{branch_name}"],
            cwd=self.repo_dir,
        )

    def write_file(self, relative_path: str, content: str) -> None:
        """Write content to a file inside the repo."""
        target = self.repo_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        logger.info("Wrote %s (%d bytes)", relative_path, len(content))

    def commit_all(self, message: str) -> None:
        """Stage all changes and create a commit."""
        _run(["git", "add", "-A"], cwd=self.repo_dir)
        result = _run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=self.repo_dir,
            check=False,
        )
        if result.returncode == 0:
            logger.info("Nothing to commit – working tree is clean")
            return
        _run(["git", "commit", "-m", message], cwd=self.repo_dir)

    def push(self, branch_name: str) -> None:
        """Push the current branch to origin."""
        _run(
            ["git", "push", "--set-upstream", "origin", branch_name],
            cwd=self.repo_dir,
            env={"GIT_TERMINAL_PROMPT": "0"},
        )

    def has_changes(self) -> bool:
        """Return True if the working tree has uncommitted changes."""
        result = _run(
            ["git", "status", "--porcelain"],
            cwd=self.repo_dir,
            capture=True,
            check=False,
        )
        return bool(result.stdout.strip())

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def run_tests(self) -> tuple[bool, str]:
        """Attempt to run the test suite; returns (passed, output)."""
        repo = self.repo_dir

        if self._has_pytest(repo):
            result = _run(
                [sys.executable, "-m", "pytest", "-q", "--tb=short"],
                cwd=repo,
                capture=True,
                check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return result.returncode == 0, output

        if (repo / "package.json").exists():
            result = _run(
                ["npm", "test", "--if-present"],
                cwd=repo,
                capture=True,
                check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return result.returncode == 0, output

        if (repo / "go.mod").exists():
            result = _run(
                ["go", "test", "./..."],
                cwd=repo,
                capture=True,
                check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return result.returncode == 0, output

        logger.info("No recognizable test suite found - skipping tests")
        return True, ""

    @staticmethod
    def _has_pytest(repo: Path) -> bool:
        """Return True if the project is configured to use pytest."""
        if (repo / "pytest.ini").exists():
            return True
        if (repo / "setup.cfg").exists():
            content = (repo / "setup.cfg").read_text(encoding="utf-8", errors="ignore")
            if "[tool:pytest]" in content:
                return True
        if (repo / "pyproject.toml").exists():
            content = (repo / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
            if "[tool.pytest" in content:
                return True
        # Also treat a tests/ or test/ directory alongside Python files as pytest
        if (repo / "tests").is_dir() or (repo / "test").is_dir():
            py_files = list(repo.glob("*.py")) + list(repo.glob("src/**/*.py"))
            if py_files:
                return True
        return False

    # ------------------------------------------------------------------
    # Branch name helpers
    # ------------------------------------------------------------------

    @staticmethod
    def issue_branch(issue_iid: int, title: str) -> str:
        slug = _slugify(title)
        return f"ai/issue-{issue_iid}-{slug}"

    @staticmethod
    def mr_fix_branch(mr_iid: int) -> str:
        return f"ai/mr-{mr_iid}-fix"
