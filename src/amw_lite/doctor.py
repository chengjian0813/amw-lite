"""Read-only workspace diagnostics (`amw-lite doctor`)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from amw_lite.config import (
    list_requirements,
    list_tasks,
    load_local_config,
    load_repositories,
    load_workspace,
)
from amw_lite.errors import AmwLiteError
from amw_lite.git import GitClient, remote_identity
from amw_lite.hooks import effective_hook
from amw_lite.security import scan_text_for_secrets
from amw_lite.skills import check_skills
from amw_lite.state import detect_dependency_cycle


@dataclass(frozen=True)
class Diagnostic:
    severity: str  # ERROR | WARN | INFO
    code: str
    message: str
    target: str = ""

    def to_dict(self) -> dict[str, str]:
        data = {"severity": self.severity, "code": self.code, "message": self.message}
        if self.target:
            data["target"] = self.target
        return data


def run_doctor(root: Path, git: GitClient | None = None) -> tuple[Diagnostic, ...]:
    """Run read-only environment, git, skill, hook, dependency, and secret diagnostics."""
    client = git or GitClient()
    diags: list[Diagnostic] = []

    if shutil.which("git") is None:
        diags.append(Diagnostic("ERROR", "GIT_MISSING", "git binary not found in PATH"))
    try:
        ws = load_workspace(root)
        repos = load_repositories(root)
    except AmwLiteError as exc:
        diags.append(Diagnostic("ERROR", exc.code, exc.message))
        return tuple(diags)

    diags.append(Diagnostic("INFO", "WORKSPACE_OK", f"workspace {ws.workspace_id} ({len(repos)} repositories)"))

    # Check local.yaml & source checkouts
    try:
        local = load_local_config(root)
    except AmwLiteError as exc:
        diags.append(Diagnostic("ERROR", "LOCAL_CONFIG_MISSING", f"{exc.message} (copy .workspace/local.example.yaml to .workspace/local.yaml)"))
        local = None

    if local is not None:
        for repo in repos:
            raw_path = local.repositories.get(repo.repo_id)
            if not raw_path:
                diags.append(Diagnostic("ERROR", "LOCAL_REPO_UNMAPPED", "missing path in .workspace/local.yaml", repo.repo_id))
                continue
            src = Path(raw_path)
            if not src.is_dir():
                diags.append(Diagnostic("ERROR", "SOURCE_MISSING", f"directory does not exist: {src}", repo.repo_id))
                continue
            if repo.remote:
                actual = remote_identity(client.remote_url(src, "origin"))
                expected = remote_identity(repo.remote)
                if actual and actual != expected:
                    diags.append(Diagnostic("ERROR", "REMOTE_MISMATCH", f"expected {expected}, got {actual}", repo.repo_id))

    # Check Skill sync
    try:
        skill_issues = check_skills(root)
        for issue in skill_issues:
            diags.append(Diagnostic("ERROR", issue.code, issue.message, issue.skill))
    except AmwLiteError as exc:
        diags.append(Diagnostic("ERROR", exc.code, exc.message))

    # Check external hooks availability (non-blocking WARN if optional CLI not installed)
    for hook_name in ("review", "e2e", "release"):
        hook = effective_hook(root, hook_name)
        if hook.enabled and hook.argv:
            binary = hook.argv[0]
            if shutil.which(binary) is None:
                hint = " (install via `npm install -g @alibaba-group/open-code-review`)" if binary == "ocr" else ""
                diags.append(
                    Diagnostic(
                        "WARN",
                        "HOOK_BINARY_MISSING",
                        f"enabled {hook_name} hook binary `{binary}` not found in PATH{hint}",
                        hook_name,
                    )
                )
            else:
                diags.append(Diagnostic("INFO", "HOOK_READY", f"{hook_name} hook binary `{binary}` is available", hook_name))

    # Check Requirement DAGs and scan committed config/specs for secrets or absolute paths
    for req in list_requirements(root):
        tasks = {t.task_id: t for t in list_tasks(root, req.requirement_id)}
        cycle = detect_dependency_cycle(tasks)
        if cycle:
            diags.append(Diagnostic("ERROR", "DEPENDENCY_CYCLE", f"cycle in {req.requirement_id}: {' -> '.join(cycle)}", req.requirement_id))

    scan_dirs = [root / ".workspace", root / "requirements", root / "contracts", root / "context"]
    for base in scan_dirs:
        if not base.is_dir():
            continue
        for file_path in sorted(base.rglob("*")):
            if not file_path.is_file() or file_path.suffix not in (".yaml", ".yml", ".md", ".json"):
                continue
            rel = file_path.relative_to(root).as_posix()
            if rel == ".workspace/local.yaml" or rel.startswith(".workspace/runtime/"):
                continue
            findings = scan_text_for_secrets(file_path.read_text(encoding="utf-8", errors="ignore"), path_label=rel)
            for finding in findings:
                diags.append(Diagnostic("ERROR", finding.code, f"{finding.path}:{finding.line} {finding.message}", finding.path))

    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    return tuple(sorted(diags, key=lambda d: (order[d.severity], d.target, d.code)))
