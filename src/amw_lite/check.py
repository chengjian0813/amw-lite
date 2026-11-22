"""Commit-bound multi-repository parallel verification with reuse and security scan."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from amw_lite.config import load_repositories, save_task
from amw_lite.errors import EXIT_EXECUTION_ERROR, EXIT_SAFETY_BLOCK, EXIT_VALIDATION_ERROR, AmwLiteError
from amw_lite.git import GitClient
from amw_lite.models import CheckEvidence, Task, VerifyCommand
from amw_lite.security import SecurityFinding, scan_text_for_secrets
from amw_lite.state import effective_verify_commands
from amw_lite.worktree import run_grouped_parallel, task_worktree_path


@dataclass(frozen=True)
class CommandOutcome:
    """Execution or reuse outcome of one verification command."""

    repo_id: str
    command: str
    commit: str
    exit_code: int
    reused: bool
    executed_at: str
    output_tail: str = ""


@dataclass(frozen=True)
class CheckReport:
    """Aggregated verification report across all repositories of a Task."""

    outcomes: tuple[CommandOutcome, ...]
    security_findings: tuple[SecurityFinding, ...]
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.errors and not self.security_findings and all(o.exit_code == 0 for o in self.outcomes)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def scan_workspace_specs_security(root: Path, req_id: str, task_id: str) -> tuple[SecurityFinding, ...]:
    """Scan requirement and task YAML/Markdown files for leaked secrets or absolute paths."""
    req_dir = root / "requirements" / req_id
    findings: list[SecurityFinding] = []
    targets = [
        req_dir / "requirement.yaml",
        req_dir / "brief.md",
        req_dir / "design.md",
        req_dir / "tasks" / task_id / "task.yaml",
        req_dir / "tasks" / task_id / "plan.md",
    ]
    for path in targets:
        if path.is_file():
            rel = str(path.relative_to(root))
            findings.extend(scan_text_for_secrets(path.read_text(encoding="utf-8"), path_label=rel))
    return tuple(findings)


def run_task_check(
    root: Path,
    task: Task,
    *,
    force: bool = False,
    git: GitClient | None = None,
    timeout_seconds: int = 600,
) -> CheckReport:
    """Run repo-bound verification commands in parallel with commit-scoped reuse."""
    if task.status != "active":
        raise AmwLiteError(
            f"task check requires active status (current: {task.status})",
            code="TASK_NOT_ACTIVE",
            exit_code=EXIT_SAFETY_BLOCK,
        )
    client = git or GitClient()
    repos_by_id = {r.repo_id: r for r in load_repositories(root)}
    commands = effective_verify_commands(task, repos_by_id)

    snapshots: dict[str, tuple[Path, str, str]] = {}
    preflight_errors: dict[str, str] = {}
    for repo_id in task.repositories:
        exec_info = task.executions.get(repo_id)
        if not exec_info:
            preflight_errors[repo_id] = f"{repo_id}: missing execution baseline metadata"
            continue
        wt = task_worktree_path(root, task.requirement_id, task.task_id, repo_id)
        if not wt.is_dir():
            preflight_errors[repo_id] = f"{repo_id}: worktree missing at {wt}"
            continue
        if client.is_dirty(wt):
            preflight_errors[repo_id] = f"{repo_id}: worktree has uncommitted changes ({wt})"
            continue
        branch = client.current_branch(wt)
        if branch != exec_info.branch:
            preflight_errors[repo_id] = f"{repo_id}: branch mismatch ({branch} != {exec_info.branch})"
            continue
        snapshots[repo_id] = (wt, branch, client.current_head(wt))

    security_findings = scan_workspace_specs_security(root, task.requirement_id, task.task_id)

    reusable: dict[tuple[str, str, str], CheckEvidence] = {
        (ev.repo_id, ev.command, ev.commit): ev
        for ev in task.checks
        if ev.is_trusted_pass
    }

    # Group commands by repo_id; each repo runs its commands serially and stops on first failure.
    cmds_by_repo: dict[str, list[VerifyCommand]] = {r: [] for r in task.repositories}
    for cmd in commands:
        if cmd.repo_id in cmds_by_repo:
            cmds_by_repo[cmd.repo_id].append(cmd)

    repo_list = [r for r in task.repositories if r not in preflight_errors]

    def run_repo(repo_id: str) -> tuple[list[CommandOutcome], list[CheckEvidence], list[str]]:
        wt, expected_branch, commit = snapshots[repo_id]
        outcomes: list[CommandOutcome] = []
        evidences: list[CheckEvidence] = []
        errs: list[str] = []
        for vc in cmds_by_repo.get(repo_id, []):
            prev = reusable.get((repo_id, vc.command, commit))
            if not force and prev is not None:
                outcomes.append(
                    CommandOutcome(
                        repo_id=repo_id,
                        command=vc.command,
                        commit=commit,
                        exit_code=0,
                        reused=True,
                        executed_at=prev.executed_at,
                    )
                )
                evidences.append(prev)
                continue
            try:
                argv = shlex.split(vc.command)
            except ValueError as exc:
                errs.append(f"{repo_id}: invalid verify command syntax ({exc})")
                break
            if not argv:
                errs.append(f"{repo_id}: empty verify command")
                break
            pre_clean = not client.is_dirty(wt)
            pre_head = client.current_head(wt)
            if not pre_clean or pre_head != commit or client.current_branch(wt) != expected_branch:
                errs.append(f"{repo_id}: worktree snapshot drifted before executing {vc.command}")
                break
            try:
                proc = subprocess.run(
                    argv,
                    cwd=str(wt),
                    text=True,
                    capture_output=True,
                    check=False,
                    shell=False,
                    timeout=timeout_seconds,
                )
            except FileNotFoundError:
                errs.append(f"{repo_id}: binary not found for command: {argv[0]}")
                break
            except subprocess.TimeoutExpired:
                errs.append(f"{repo_id}: command timed out: {vc.command}")
                break
            post_clean = not client.is_dirty(wt)
            post_head = client.current_head(wt)
            if not post_clean or post_head != commit or client.current_branch(wt) != expected_branch:
                errs.append(f"{repo_id}: worktree snapshot mutated during {vc.command}")
                break
            executed_at = _now_iso()
            tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-1000:]
            ev = CheckEvidence(
                repo_id=repo_id,
                command=vc.command,
                commit=commit,
                exit_code=proc.returncode,
                clean_before=pre_clean,
                clean_after=post_clean,
                executed_at=executed_at,
            )
            evidences.append(ev)
            outcomes.append(
                CommandOutcome(
                    repo_id=repo_id,
                    command=vc.command,
                    commit=commit,
                    exit_code=proc.returncode,
                    reused=False,
                    executed_at=executed_at,
                    output_tail=tail,
                )
            )
            if proc.returncode != 0:
                errs.append(f"{repo_id}: command failed (exit {proc.returncode}): {vc.command}")
                break
        return outcomes, evidences, errs

    grouped = run_grouped_parallel(repo_list, group_key=lambda r: r, worker=run_repo)

    all_outcomes: list[CommandOutcome] = []
    settled_evidence: dict[tuple[str, str], CheckEvidence] = {
        (ev.repo_id, ev.command): ev for ev in task.checks
    }
    all_errors: list[str] = [preflight_errors[r] for r in task.repositories if r in preflight_errors]

    for repo_id, val, exc in grouped:
        if exc is not None:
            all_errors.append(f"{repo_id}: {exc}")
            continue
        assert val is not None
        outcomes, evidences, errs = val
        all_outcomes.extend(outcomes)
        all_errors.extend(errs)
        for ev in evidences:
            settled_evidence[(ev.repo_id, ev.command)] = ev

    # Persist all settled repository evidence even if another repo failed (never lose valid results).
    ordered_evidence = tuple(
        settled_evidence[k]
        for k in sorted(settled_evidence.keys())
    )
    if ordered_evidence != task.checks:
        save_task(root, replace(task, checks=ordered_evidence))

    if security_findings:
        all_errors.append(f"security scan found {len(security_findings)} issue(s) in spec/task files")

    report = CheckReport(
        outcomes=tuple(all_outcomes),
        security_findings=security_findings,
        errors=tuple(all_errors),
    )
    if not report.passed:
        code = "SECURITY_SCAN_FAILED" if security_findings else "CHECK_FAILED"
        exit_code = EXIT_VALIDATION_ERROR if security_findings else EXIT_EXECUTION_ERROR
        raise AmwLiteError(
            "; ".join(report.errors) or "verification check failed",
            code=code,
            exit_code=exit_code,
            details={
                "outcomes": [
                    {
                        "repo_id": o.repo_id,
                        "command": o.command,
                        "commit": o.commit,
                        "exit_code": o.exit_code,
                        "reused": o.reused,
                    }
                    for o in report.outcomes
                ],
                "security_findings": [
                    {"code": f.code, "path": f.path, "line": f.line, "message": f.message}
                    for f in report.security_findings
                ],
            },
        )
    return report
