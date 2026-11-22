"""Open external CLI hook runner: Alibaba Open Code Review (ocr), E2E, and Release hooks."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from amw_lite.config import (
    atomic_write_text,
    list_tasks,
    load_local_config,
    load_repositories,
    load_requirement,
    load_workspace,
    requirement_dir,
    save_task,
)
from amw_lite.errors import (
    EXIT_DEPENDENCY_ERROR,
    EXIT_EXECUTION_ERROR,
    EXIT_SAFETY_BLOCK,
    EXIT_VALIDATION_ERROR,
    AmwLiteError,
)
from amw_lite.git import GitClient
from amw_lite.models import HookConfig, ReviewEvidence, Task
from amw_lite.security import find_unresolved_placeholders
from amw_lite.state import checks_pass_for_commits, effective_verify_commands
from amw_lite.worktree import task_worktree_path

_VAR_TOKEN_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")
_MAX_OUTPUT_CHARS = 32_000


@dataclass(frozen=True)
class RepoReviewSummary:
    """Normalized review summary for one repository at current HEAD."""

    repo_id: str
    tool: str
    commit: str
    findings_count: int
    human_approved: bool
    report_path: str
    summary_text: str


@dataclass(frozen=True)
class HookPreview:
    """Exact rendered argv preview for an external hook command."""

    hook_name: str
    argv: tuple[str, ...]
    cwd: str
    applied: bool
    exit_code: int | None = None
    output_tail: str = ""
    note: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_hook_argv(template: tuple[str, ...], variables: dict[str, str]) -> tuple[str, ...]:
    """Expand allow-listed `{var}` placeholders per argv element and reject unresolved ones."""
    if not template:
        raise AmwLiteError("hook argv template is empty", code="HOOK_NOT_CONFIGURED", exit_code=EXIT_VALIDATION_ERROR)
    rendered: list[str] = []
    for arg in template:
        def _sub(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in variables:
                raise AmwLiteError(
                    f"unknown hook template variable: {{{key}}}",
                    code="UNRESOLVED_HOOK_VARIABLE",
                    exit_code=EXIT_VALIDATION_ERROR,
                )
            return variables[key]

        value = _VAR_TOKEN_RE.sub(_sub, arg)
        leftover = find_unresolved_placeholders(value)
        if leftover:
            raise AmwLiteError(
                f"hook argument contains unresolved placeholder(s): {', '.join(leftover)}",
                code="UNRESOLVED_PLACEHOLDER",
                exit_code=EXIT_VALIDATION_ERROR,
            )
        rendered.append(value)
    return tuple(rendered)


def effective_hook(root: Path, name: str) -> HookConfig:
    ws = load_workspace(root)
    base = ws.hooks.get(name, HookConfig())
    try:
        local = load_local_config(root)
    except AmwLiteError:
        local = None
    if local and name in local.hooks_override:
        return HookConfig(
            enabled=True,
            argv=tuple(local.hooks_override[name]),
            timeout_seconds=base.timeout_seconds,
            policy=dict(base.policy),
        )
    return base


def _build_review_background(root: Path, req_id: str, task: Task) -> str:
    """Build a concise, safe background summary from brief.md and design.md for `ocr --background`."""
    rdir = requirement_dir(root, req_id)
    chunks = [f"Requirement {req_id} / Task {task.task_id}: {task.title}"]
    for fname in ("brief.md", "design.md"):
        p = rdir / fname
        if p.is_file():
            snippet = " ".join(p.read_text(encoding="utf-8").split())[:500]
            if snippet:
                chunks.append(f"{fname}: {snippet}")
    return " | ".join(chunks)[:1200]


def _extract_ocr_findings_count(raw_stdout: str) -> int:
    """Best-effort count of review findings from `ocr review --format json` output."""
    try:
        parsed: Any = json.loads(raw_stdout)
    except json.JSONDecodeError:
        return 0
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict):
        for key in ("findings", "comments", "issues", "reviews", "items"):
            val = parsed.get(key)
            if isinstance(val, list):
                return len(val)
        if isinstance(parsed.get("total"), int):
            return int(parsed["total"])
    return 0


def run_task_review(
    root: Path,
    task: Task,
    *,
    approve: bool = False,
    git: GitClient | None = None,
) -> tuple[RepoReviewSummary, ...]:
    """Run external Code Review hook (default: Alibaba `ocr`) or record human approval at current HEAD."""
    if task.status != "active":
        raise AmwLiteError(
            f"task review requires active status (current: {task.status})",
            code="TASK_NOT_ACTIVE",
            exit_code=EXIT_SAFETY_BLOCK,
        )
    client = git or GitClient()
    repos_by_id = {r.repo_id: r for r in load_repositories(root)}
    commits_by_repo: dict[str, str] = {}
    worktrees_by_repo: dict[str, Path] = {}

    for repo_id in task.repositories:
        exec_info = task.executions.get(repo_id)
        if not exec_info:
            raise AmwLiteError(f"missing execution metadata for {repo_id}", code="EXECUTION_MISSING", exit_code=EXIT_SAFETY_BLOCK)
        wt = task_worktree_path(root, task.requirement_id, task.task_id, repo_id)
        if not wt.is_dir():
            raise AmwLiteError(f"worktree missing for {repo_id}: {wt}", code="WORKTREE_MISSING", exit_code=EXIT_SAFETY_BLOCK)
        if client.is_dirty(wt):
            raise AmwLiteError(
                f"dirty worktree blocks review for {repo_id}; commit changes first",
                code="WORKTREE_DIRTY",
                exit_code=EXIT_SAFETY_BLOCK,
            )
        if client.current_branch(wt) != exec_info.branch:
            raise AmwLiteError(f"branch drift detected for {repo_id}", code="BRANCH_DRIFT", exit_code=EXIT_SAFETY_BLOCK)
        worktrees_by_repo[repo_id] = wt
        commits_by_repo[repo_id] = client.current_head(wt)

    commands = effective_verify_commands(task, repos_by_id)
    if not checks_pass_for_commits(task, commands, commits_by_repo):
        raise AmwLiteError(
            "task check must pass at current HEAD before code review",
            code="CHECK_REQUIRED",
            exit_code=EXIT_SAFETY_BLOCK,
        )

    existing_by_key = {(r.repo_id, r.commit): r for r in task.reviews}

    if approve:
        missing = [r for r in task.repositories if (r, commits_by_repo[r]) not in existing_by_key]
        if missing:
            raise AmwLiteError(
                f"run `amw-lite task review {task.requirement_id} {task.task_id}` before --approve (missing review for: {', '.join(missing)})",
                code="REVIEW_NOT_RUN",
                exit_code=EXIT_SAFETY_BLOCK,
            )
        now = _now_iso()
        updated_map = dict(existing_by_key)
        summaries: list[RepoReviewSummary] = []
        for repo_id in task.repositories:
            commit = commits_by_repo[repo_id]
            prev = existing_by_key[(repo_id, commit)]
            rec = replace(prev, human_approved=True, reviewed_at=now)
            updated_map[(repo_id, commit)] = rec
            summaries.append(
                RepoReviewSummary(
                    repo_id=repo_id,
                    tool=rec.tool,
                    commit=commit,
                    findings_count=rec.findings_count,
                    human_approved=True,
                    report_path=rec.report_path,
                    summary_text="Human Code Review approved at current HEAD.",
                )
            )
        save_task(root, replace(task, reviews=tuple(updated_map.values())))
        return tuple(summaries)

    hook = effective_hook(root, "review")
    background = _build_review_background(root, task.requirement_id, task)
    review_dir = root / ".workspace" / "runtime" / "reviews" / task.requirement_id / task.task_id
    review_dir.mkdir(parents=True, exist_ok=True)

    updated_reviews = dict(existing_by_key)
    summaries = []

    for repo_id in task.repositories:
        wt = worktrees_by_repo[repo_id]
        commit = commits_by_repo[repo_id]
        baseline_commit = task.executions[repo_id].baseline_commit
        report_file = review_dir / f"{repo_id}-{commit[:8]}.txt"
        rel_report = str(report_file.relative_to(root))

        use_external = hook.enabled and bool(hook.argv) and shutil.which(hook.argv[0]) is not None
        if use_external:
            argv = resolve_hook_argv(
                hook.argv,
                {
                    "req_id": task.requirement_id,
                    "task_id": task.task_id,
                    "repo_id": repo_id,
                    "baseline_commit": baseline_commit,
                    "head_commit": commit,
                    "worktree_path": str(wt),
                    "review_background": background,
                    "design_path": str((requirement_dir(root, task.requirement_id) / "design.md").resolve()),
                    "workspace_root": str(root.resolve()),
                },
            )
            try:
                proc = subprocess.run(
                    list(argv),
                    cwd=str(wt),
                    text=True,
                    capture_output=True,
                    check=False,
                    shell=False,
                    timeout=hook.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise AmwLiteError(f"review hook timed out for {repo_id}", code="REVIEW_TIMEOUT", exit_code=EXIT_DEPENDENCY_ERROR) from exc
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).strip()[:500]
                raise AmwLiteError(
                    f"review hook ({argv[0]}) failed for {repo_id} (exit {proc.returncode}): {detail}",
                    code="REVIEW_HOOK_FAILED",
                    exit_code=EXIT_EXECUTION_ERROR,
                )
            raw_out = proc.stdout[:_MAX_OUTPUT_CHARS]
            report_file = review_dir / f"{repo_id}-{commit[:8]}.json"
            rel_report = str(report_file.relative_to(root))
            atomic_write_text(report_file, raw_out)
            tool_name = Path(argv[0]).name
            findings_count = _extract_ocr_findings_count(raw_out)
            summary_text = f"{tool_name} completed with {findings_count} finding(s); report: {rel_report}"
        else:
            diff_summary = client.diff_stat(wt, baseline_commit, commit) or "(no diff)"
            atomic_write_text(report_file, diff_summary + "\n")
            tool_name = "diff-stat"
            findings_count = 0
            note = " (fallback: review CLI not installed or disabled)" if hook.enabled else ""
            summary_text = f"diff-stat generated{note}:\n{diff_summary}"

        rec = ReviewEvidence(
            repo_id=repo_id,
            tool=tool_name,
            commit=commit,
            findings_count=findings_count,
            human_approved=False,
            reviewed_at=_now_iso(),
            report_path=rel_report,
        )
        updated_reviews[(repo_id, commit)] = rec
        summaries.append(
            RepoReviewSummary(
                repo_id=repo_id,
                tool=tool_name,
                commit=commit,
                findings_count=findings_count,
                human_approved=False,
                report_path=rel_report,
                summary_text=summary_text,
            )
        )

    save_task(root, replace(task, reviews=tuple(updated_reviews.values())))
    return tuple(summaries)


def run_e2e_hook(root: Path, req_id: str, *, apply: bool = False) -> HookPreview:
    """Preview or execute the optional E2E hook without forcing prior MR merge."""
    req = load_requirement(root, req_id)
    if not req.gates.brief_approved or not req.gates.design_approved:
        raise AmwLiteError("brief and design gates must be approved before E2E", code="GATE_REQUIRED", exit_code=EXIT_SAFETY_BLOCK)
    if not req.gates.e2e_plan_approved:
        raise AmwLiteError(
            f"e2e-plan gate must be approved first: amw-lite req approve {req_id} --gate e2e-plan",
            code="E2E_PLAN_NOT_APPROVED",
            exit_code=EXIT_SAFETY_BLOCK,
        )
    e2e_plan_path = requirement_dir(root, req_id) / "e2e-plan.md"
    hook = effective_hook(root, "e2e")
    if not hook.enabled or not hook.argv:
        return HookPreview(
            hook_name="e2e",
            argv=(),
            cwd=str(root.resolve()),
            applied=False,
            note=f"hooks.e2e is not configured; execute cases in {e2e_plan_path.relative_to(root)} manually or via Agent, then run `amw-lite req approve {req_id} --gate e2e-result`.",
        )
    argv = resolve_hook_argv(
        hook.argv,
        {
            "req_id": req_id,
            "e2e_plan_path": str(e2e_plan_path.resolve()),
            "workspace_root": str(root.resolve()),
        },
    )
    if not apply:
        return HookPreview(
            hook_name="e2e",
            argv=argv,
            cwd=str(root.resolve()),
            applied=False,
            note="Run again with --apply to execute the E2E hook.",
        )
    if shutil.which(argv[0]) is None:
        raise AmwLiteError(f"E2E hook binary not found: {argv[0]}", code="HOOK_BINARY_MISSING", exit_code=EXIT_DEPENDENCY_ERROR)
    proc = subprocess.run(
        list(argv),
        cwd=str(root.resolve()),
        text=True,
        capture_output=True,
        check=False,
        shell=False,
        timeout=hook.timeout_seconds,
    )
    out_dir = root / ".workspace" / "runtime" / "e2e"
    out_dir.mkdir(parents=True, exist_ok=True)
    combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[:_MAX_OUTPUT_CHARS]
    atomic_write_text(out_dir / f"{req_id}.log", combined + "\n")
    if proc.returncode != 0:
        raise AmwLiteError(
            f"E2E hook failed (exit {proc.returncode}); fix bugs in current branch and re-run check/review/e2e",
            code="E2E_FAILED",
            exit_code=EXIT_EXECUTION_ERROR,
            details={"output_tail": combined[-1000:]},
        )
    return HookPreview(
        hook_name="e2e",
        argv=argv,
        cwd=str(root.resolve()),
        applied=True,
        exit_code=0,
        output_tail=combined[-1000:],
        note=f"E2E hook passed. Inspect results and confirm Gate #5: amw-lite req approve {req_id} --gate e2e-result",
    )


def run_release_hook(root: Path, req_id: str, env: str, *, apply: bool = False) -> HookPreview:
    """Preview or run external QA/PRE release hook; PROD is strictly manual-only."""
    env_lower = env.lower().strip()
    if env_lower == "prod":
        raise AmwLiteError(
            "PROD deployment is manual-only and permanently blocked from automatic execution; record manual results in release-plan.md",
            code="PROD_AUTO_DEPLOY_FORBIDDEN",
            exit_code=EXIT_SAFETY_BLOCK,
        )
    if env_lower not in ("qa", "pre"):
        raise AmwLiteError(f"unsupported environment: {env} (expected qa, pre, or prod)", code="INVALID_ENV", exit_code=EXIT_VALIDATION_ERROR)

    req = load_requirement(root, req_id)
    tasks = [t for t in list_tasks(root, req_id) if t.status != "cancelled"]
    if not tasks or any(t.status != "done" for t in tasks):
        pending = [t.task_id for t in tasks if t.status != "done"]
        raise AmwLiteError(
            f"all Tasks must be done (merged) before release run; pending: {', '.join(pending) or 'none'}",
            code="TASKS_NOT_DONE",
            exit_code=EXIT_SAFETY_BLOCK,
        )
    if not req.gates.e2e_result_approved:
        raise AmwLiteError("e2e-result gate must be approved before release run", code="E2E_NOT_APPROVED", exit_code=EXIT_SAFETY_BLOCK)
    if not req.gates.release_plan_approved:
        raise AmwLiteError("release-plan gate must be approved before release run", code="RELEASE_PLAN_NOT_APPROVED", exit_code=EXIT_SAFETY_BLOCK)

    hook = effective_hook(root, "release")
    env_policy = hook.policy.get(env_lower, "ask")
    if env_policy == "off" or not hook.enabled or not hook.argv:
        return HookPreview(
            hook_name="release",
            argv=(),
            cwd=str(root.resolve()),
            applied=False,
            note=f"Release hook for {env_lower} is disabled (policy={env_policy}); execute deployment manually using requirements/{req_id}/release-plan.md.",
        )

    plan_path = requirement_dir(root, req_id) / "release-plan.md"
    argv = resolve_hook_argv(
        hook.argv,
        {
            "req_id": req_id,
            "env": env_lower,
            "release_plan_path": str(plan_path.resolve()),
            "workspace_root": str(root.resolve()),
        },
    )
    if not apply:
        return HookPreview(
            hook_name="release",
            argv=argv,
            cwd=str(root.resolve()),
            applied=False,
            note=f"Review exact {env_lower} release command, then re-run with --apply after explicit human confirmation.",
        )
    if shutil.which(argv[0]) is None:
        raise AmwLiteError(f"release hook binary not found: {argv[0]}", code="HOOK_BINARY_MISSING", exit_code=EXIT_DEPENDENCY_ERROR)
    proc = subprocess.run(
        list(argv),
        cwd=str(root.resolve()),
        text=True,
        capture_output=True,
        check=False,
        shell=False,
        timeout=hook.timeout_seconds,
    )
    combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[:_MAX_OUTPUT_CHARS]
    if proc.returncode != 0:
        raise AmwLiteError(
            f"release hook failed for {env_lower} (exit {proc.returncode})",
            code="RELEASE_HOOK_FAILED",
            exit_code=EXIT_EXECUTION_ERROR,
            details={"output_tail": combined[-1000:]},
        )
    return HookPreview(
        hook_name="release",
        argv=argv,
        cwd=str(root.resolve()),
        applied=True,
        exit_code=0,
        output_tail=combined[-1000:],
        note=f"{env_lower} release hook succeeded.",
    )
