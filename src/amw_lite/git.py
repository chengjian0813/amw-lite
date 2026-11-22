"""Thin argv-only Git CLI wrapper (no shell=True, no destructive resets)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from amw_lite.errors import EXIT_DEPENDENCY_ERROR, EXIT_SAFETY_BLOCK, AmwLiteError


class GitClient:
    """Execute read-only and safe worktree Git operations via argv lists."""

    def __init__(self, timeout_seconds: int = 120) -> None:
        self._timeout = timeout_seconds

    def run(self, repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = ["git", "-C", str(repo), *args]
        try:
            proc = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                check=False,
                shell=False,
                timeout=self._timeout,
            )
        except FileNotFoundError as exc:
            raise AmwLiteError("git binary not found in PATH", code="GIT_NOT_FOUND", exit_code=EXIT_DEPENDENCY_ERROR) from exc
        except subprocess.TimeoutExpired as exc:
            raise AmwLiteError(f"git command timed out: {' '.join(args)}", code="GIT_TIMEOUT", exit_code=EXIT_DEPENDENCY_ERROR) from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
            raise AmwLiteError(f"git {' '.join(args)} failed in {repo}: {detail}", code="GIT_FAILED", exit_code=EXIT_SAFETY_BLOCK)
        return proc

    def current_head(self, repo: Path) -> str:
        return self.run(repo, "rev-parse", "HEAD").stdout.strip()

    def current_branch(self, repo: Path) -> str:
        proc = self.run(repo, "symbolic-ref", "--short", "HEAD", check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def is_dirty(self, repo: Path) -> bool:
        return bool(self.run(repo, "status", "--porcelain").stdout.strip())

    def has_tracked_changes(self, repo: Path) -> bool:
        out = self.run(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()
        return bool(out)

    def remote_url(self, repo: Path, remote: str = "origin") -> str:
        proc = self.run(repo, "remote", "get-url", remote, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def branch_exists(self, repo: Path, branch: str) -> bool:
        proc = self.run(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        return proc.returncode == 0

    def resolve_commit(self, repo: Path, ref: str) -> str:
        return self.run(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()

    def remote_branch_head(self, repo: Path, remote: str, branch: str) -> str:
        out = self.run(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}").stdout.strip()
        if not out:
            raise AmwLiteError(
                f"remote branch {remote}/{branch} not found in {repo}",
                code="REMOTE_BRANCH_MISSING",
                exit_code=EXIT_SAFETY_BLOCK,
            )
        return out.splitlines()[0].split()[0]

    def fetch_branch_head(self, repo: Path, remote: str, branch: str) -> str:
        self.run(repo, "fetch", remote, f"refs/heads/{branch}:refs/remotes/{remote}/{branch}")
        return self.resolve_commit(repo, f"refs/remotes/{remote}/{branch}")

    def is_ancestor(self, repo: Path, ancestor: str, descendant: str) -> bool:
        proc = self.run(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
        return proc.returncode == 0

    def fast_forward_branch_ref(self, repo: Path, branch: str, target_commit: str) -> None:
        """Advance a non-checked-out local branch ref only if fast-forward."""
        if self.branch_exists(repo, branch):
            local = self.resolve_commit(repo, f"refs/heads/{branch}")
            if local == target_commit:
                return
            if not self.is_ancestor(repo, local, target_commit):
                raise AmwLiteError(
                    f"local branch {branch} diverged from remote ({local[:8]} vs {target_commit[:8]})",
                    code="BASELINE_DIVERGED",
                    exit_code=EXIT_SAFETY_BLOCK,
                )
        self.run(repo, "branch", "-f", branch, target_commit)

    def fast_forward_checked_out(self, worktree: Path, target_commit: str) -> None:
        self.run(worktree, "merge", "--ff-only", target_commit)

    def worktree_list(self, repo: Path) -> list[tuple[Path, str]]:
        """Return list of `(worktree_path, branch_name)` registered for `repo`."""
        out = self.run(repo, "worktree", "list", "--porcelain").stdout
        entries: list[tuple[Path, str]] = []
        current_path: Path | None = None
        current_branch = ""
        for line in out.splitlines() + [""]:
            if line.startswith("worktree "):
                current_path = Path(line[len("worktree ") :].strip()).resolve()
                current_branch = ""
            elif line.startswith("branch refs/heads/"):
                current_branch = line[len("branch refs/heads/") :].strip()
            elif not line.strip() and current_path is not None:
                entries.append((current_path, current_branch))
                current_path = None
                current_branch = ""
        return entries

    def worktree_for_branch(self, repo: Path, branch: str) -> Path | None:
        for path, br in self.worktree_list(repo):
            if br == branch:
                return path
        return None

    def worktree_add(self, source_repo: Path, target: Path, branch: str, start_commit: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        self.run(source_repo, "worktree", "add", "-b", branch, str(target), start_commit)

    def worktree_remove(self, source_repo: Path, target: Path) -> None:
        self.run(source_repo, "worktree", "remove", str(target))

    def diff_stat(self, repo: Path, base_commit: str, head_commit: str = "HEAD") -> str:
        return self.run(repo, "diff", "--stat", f"{base_commit}..{head_commit}").stdout.strip()


def remote_identity(remote: str) -> str:
    """Normalize SSH/HTTPS Git remote URL to credential-free `host/path`."""
    value = remote.strip()
    if re.match(r"^[^/@:]+@[^/:]+:.+$", value):
        host_path = value.split("@", 1)[1].replace(":", "/", 1)
    elif "://" in value:
        parsed = urlsplit(value)
        host_path = f"{parsed.hostname or ''}{parsed.path}"
    else:
        host_path = value
    normalized = host_path.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized


def build_mr_url(remote: str, source_branch: str, target_branch: str) -> str:
    """Build a target-explicit GitHub PR or GitLab MR creation URL from a remote."""
    ident = remote_identity(remote)
    if not ident or "/" not in ident:
        return ""
    host = ident.split("/", 1)[0].lower()
    if "github" in host:
        return f"https://{ident}/compare/{target_branch}...{source_branch}?expand=1"
    query = urlencode(
        {
            "merge_request[source_branch]": source_branch,
            "merge_request[target_branch]": target_branch,
        }
    )
    return f"https://{ident}/-/merge_requests/new?{query}"
