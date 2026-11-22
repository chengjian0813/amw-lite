"""Core orchestration operations: init, req lifecycle, task lifecycle, release plan, resume."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from amw_lite.config import (
    atomic_write_text,
    dump_yaml,
    list_requirements,
    list_tasks,
    load_local_config,
    load_repositories,
    load_requirement,
    load_task,
    load_workspace,
    read_contract_status,
    requirement_dir,
    save_requirement,
    save_task,
    task_dir,
)
from amw_lite.errors import EXIT_SAFETY_BLOCK, EXIT_VALIDATION_ERROR, AmwLiteError
from amw_lite.git import GitClient, build_mr_url
from amw_lite.models import (
    GATE_NAMES,
    HookConfig,
    RepoExecution,
    RepositoryConfig,
    Requirement,
    RequirementGates,
    Task,
    TaskDependency,
    VerifyCommand,
    WorkspaceConfig,
)
from amw_lite.security import check_markdown_complete, require_safe_id
from amw_lite.skills import sync_skills
from amw_lite.state import (
    EffectiveStage,
    RepoWorktreeObservation,
    TaskStageResult,
    checks_pass_for_commits,
    derive_next_action,
    derive_task_stage,
    detect_dependency_cycle,
    effective_verify_commands,
    evaluate_ready_blockers,
    review_state_for_commits,
    topological_task_order,
)
from amw_lite.templates.assets import (
    AGENTS_MD_TEMPLATE,
    BRIEF_MD_TEMPLATE,
    CLAUDE_MD_TEMPLATE,
    COMPANION_SKILL_MD,
    DESIGN_MD_TEMPLATE,
    DOMAIN_CONTEXT_TEMPLATE,
    E2E_PLAN_MD_TEMPLATE,
    RELEASE_PLAN_MD_TEMPLATE,
    TASK_PLAN_MD_TEMPLATE,
    WORKSPACE_GITIGNORE,
    WORKSPACE_README_TEMPLATE,
)
from amw_lite.worktree import (
    WorktreePlan,
    create_worktrees,
    plan_cleanup_worktrees,
    plan_worktrees,
    task_worktree_path,
)


@dataclass(frozen=True)
class MrPlan:
    repo_id: str
    source_branch: str
    target_branch: str
    commit: str
    url: str
    push_command: str

    def to_dict(self) -> dict[str, str]:
        return {
            "repo_id": self.repo_id,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "commit": self.commit,
            "url": self.url,
            "push_command": self.push_command,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_hooks() -> dict[str, HookConfig]:
    """Return open-source defaults with Alibaba `ocr` CLI pre-wired for review."""
    return {
        "review": HookConfig(
            enabled=True,
            argv=(
                "ocr",
                "review",
                "--from",
                "{baseline_commit}",
                "--to",
                "{head_commit}",
                "--background",
                "{review_background}",
                "--audience",
                "agent",
                "--format",
                "json",
            ),
        ),
        "e2e": HookConfig(enabled=False, argv=()),
        "release": HookConfig(
            enabled=False,
            argv=(),
            policy={"qa": "ask", "pre": "ask", "prod": "manual"},
        ),
    }


def init_workspace(
    target: Path,
    *,
    workspace_id: str,
    name: str,
    repositories: list[dict[str, Any]],
    baseline: str = "main",
    platforms: tuple[str, ...] = ("codex", "claude", "cursor"),
    dry_run: bool = False,
) -> list[str]:
    """Initialize a fresh amw-lite Workspace without overwriting existing files."""
    ws = WorkspaceConfig(
        workspace_id=workspace_id,
        name=name,
        baseline=baseline,
        platforms=platforms,
        hooks=default_hooks(),
    )
    if not repositories:
        raise AmwLiteError("init requires at least one --repo definition", code="REPOS_REQUIRED", exit_code=EXIT_VALIDATION_ERROR)

    repo_models: list[RepositoryConfig] = []
    local_map: dict[str, str] = {}
    for item in repositories:
        repo = RepositoryConfig(
            repo_id=str(item["repo_id"]),
            name=str(item.get("name") or item["repo_id"]),
            baseline=str(item.get("baseline") or "main"),
            role=str(item.get("role") or "service"),
            remote=str(item.get("remote") or ""),
            default_verify=tuple(item.get("default_verify") or ()),
        )
        repo_models.append(repo)
        if item.get("path"):
            local_map[repo.repo_id] = str(Path(str(item["path"])).expanduser().resolve())

    files: dict[Path, str] = {
        target / ".gitignore": WORKSPACE_GITIGNORE,
        target / "README.md": WORKSPACE_README_TEMPLATE.format(name=ws.name),
        target / "AGENTS.md": AGENTS_MD_TEMPLATE.format(name=ws.name),
        target / "CLAUDE.md": CLAUDE_MD_TEMPLATE.format(name=ws.name),
        target / ".workspace" / "workspace.yaml": dump_yaml(ws.to_dict()),
        target / ".workspace" / "repositories.yaml": dump_yaml(
            {"schema_version": 1, "repositories": [r.to_dict() for r in repo_models]}
        ),
        target / ".workspace" / "local.example.yaml": dump_yaml(
            {
                "schema_version": 1,
                "worktree_root": None,
                "repositories": {r.repo_id: f"/path/to/{r.repo_id}" for r in repo_models},
                "hooks_override": {},
            }
        ),
        target / ".workspace" / "skills-src" / "amw-lite" / "SKILL.md": COMPANION_SKILL_MD,
        target / "context" / "domain.md": DOMAIN_CONTEXT_TEMPLATE,
        target / "contracts" / ".gitkeep": "",
        target / "requirements" / ".gitkeep": "",
    }
    if local_map:
        files[target / ".workspace" / "local.yaml"] = dump_yaml(
            {
                "schema_version": 1,
                "worktree_root": None,
                "repositories": local_map,
                "hooks_override": {},
            }
        )

    conflicts = [str(p.relative_to(target)) for p in files if p.exists()]
    if conflicts:
        raise AmwLiteError(
            f"target already contains managed file(s): {', '.join(conflicts)}",
            code="INIT_CONFLICT",
            exit_code=EXIT_SAFETY_BLOCK,
        )

    planned = sorted(str(p.relative_to(target)) for p in files)
    if dry_run:
        return planned

    target.mkdir(parents=True, exist_ok=True)
    for path, content in files.items():
        atomic_write_text(path, content)
    sync_skills(target, apply=True)
    return planned


def create_requirement(
    root: Path,
    req_id: str,
    title: str,
    *,
    kind: str = "feature",
    owner: str = "",
    external_ref: str = "",
) -> Requirement:
    require_safe_id("requirement_id", req_id)
    rdir = requirement_dir(root, req_id)
    if rdir.exists():
        raise AmwLiteError(f"requirement already exists: {req_id}", code="REQ_EXISTS", exit_code=EXIT_SAFETY_BLOCK)
    req = Requirement(
        requirement_id=req_id,
        title=title,
        kind=kind,
        owner=owner,
        status="open",
        external_ref=external_ref,
        gates=RequirementGates(),
    )
    save_requirement(root, req)
    atomic_write_text(rdir / "brief.md", BRIEF_MD_TEMPLATE.format(req_id=req_id, title=title))
    atomic_write_text(rdir / "design.md", DESIGN_MD_TEMPLATE.format(req_id=req_id, title=title))
    atomic_write_text(rdir / "e2e-plan.md", E2E_PLAN_MD_TEMPLATE.format(req_id=req_id, title=title))
    atomic_write_text(rdir / "release-plan.md", RELEASE_PLAN_MD_TEMPLATE.format(req_id=req_id, title=title))
    (rdir / "tasks").mkdir(parents=True, exist_ok=True)
    return req


def approve_requirement_gate(root: Path, req_id: str, gate: str) -> Requirement:
    if gate not in GATE_NAMES:
        raise AmwLiteError(
            f"unknown gate: {gate} (expected one of: {', '.join(GATE_NAMES)})",
            code="INVALID_GATE",
            exit_code=EXIT_VALIDATION_ERROR,
        )
    req = load_requirement(root, req_id)
    rdir = requirement_dir(root, req_id)
    doc_map = {
        "brief": ("brief.md", "brief_approved"),
        "design": ("design.md", "design_approved"),
        "e2e-plan": ("e2e-plan.md", "e2e_plan_approved"),
        "e2e-result": ("e2e-plan.md", "e2e_result_approved"),
        "release-plan": ("release-plan.md", "release_plan_approved"),
    }
    filename, field_name = doc_map[gate]
    err = check_markdown_complete(rdir / filename)
    if err:
        raise AmwLiteError(err, code="SPEC_INCOMPLETE", exit_code=EXIT_VALIDATION_ERROR)
    if gate == "e2e-result" and not req.gates.e2e_plan_approved:
        raise AmwLiteError("approve e2e-plan before e2e-result", code="GATE_ORDER_ERROR", exit_code=EXIT_SAFETY_BLOCK)
    updated_gates = replace(req.gates, **{field_name: True})
    updated = replace(req, gates=updated_gates)
    save_requirement(root, updated)
    return updated


def create_task(
    root: Path,
    req_id: str,
    task_id: str,
    title: str,
    repositories: tuple[str, ...],
    *,
    assignee: str = "",
    contracts: tuple[str, ...] = (),
    depends_on: tuple[TaskDependency, ...] = (),
    verify_commands: tuple[VerifyCommand, ...] = (),
    context_files: tuple[str, ...] = (),
) -> Task:
    load_requirement(root, req_id)
    known_repos = {r.repo_id for r in load_repositories(root)}
    for repo_id in repositories:
        if repo_id not in known_repos:
            raise AmwLiteError(f"unknown repo_id: {repo_id}", code="UNKNOWN_REPOSITORY", exit_code=EXIT_VALIDATION_ERROR)
    tdir = task_dir(root, req_id, task_id)
    if tdir.exists():
        raise AmwLiteError(f"task already exists: {req_id}/{task_id}", code="TASK_EXISTS", exit_code=EXIT_SAFETY_BLOCK)

    task = Task(
        requirement_id=req_id,
        task_id=task_id,
        title=title,
        repositories=repositories,
        assignee=assignee,
        status="draft",
        contracts=contracts,
        depends_on=depends_on,
        context_files=context_files,
        verify_commands=verify_commands,
    )
    existing = {t.task_id: t for t in list_tasks(root, req_id)}
    existing[task.task_id] = task
    cycle = detect_dependency_cycle(existing)
    if cycle:
        raise AmwLiteError(
            f"adding task creates dependency cycle: {' -> '.join(cycle)}",
            code="DEPENDENCY_CYCLE",
            exit_code=EXIT_VALIDATION_ERROR,
        )
    save_task(root, task)
    atomic_write_text(tdir / "plan.md", TASK_PLAN_MD_TEMPLATE.format(req_id=req_id, task_id=task_id, title=title))
    return task


def start_task(
    root: Path,
    req_id: str,
    task_id: str,
    *,
    dry_run: bool = False,
    git: GitClient | None = None,
) -> tuple[WorktreePlan, ...]:
    req = load_requirement(root, req_id)
    if not req.gates.brief_approved or not req.gates.design_approved:
        raise AmwLiteError(
            "brief and design gates must be approved before starting a Task",
            code="DESIGN_NOT_APPROVED",
            exit_code=EXIT_SAFETY_BLOCK,
        )
    task = load_task(root, req_id, task_id)
    if task.status != "draft":
        raise AmwLiteError(f"task start requires draft status (current: {task.status})", code="TASK_NOT_DRAFT", exit_code=EXIT_SAFETY_BLOCK)

    all_tasks = {t.task_id: t for t in list_tasks(root, req_id)}
    known_repos = {r.repo_id for r in load_repositories(root)}
    plan_error = check_markdown_complete(task_dir(root, req_id, task_id) / "plan.md")
    blockers = evaluate_ready_blockers(
        task,
        all_tasks,
        known_repos,
        lambda c: read_contract_status(root, c),
        plan_error=plan_error,
    )
    if blockers:
        raise AmwLiteError("; ".join(blockers), code="TASK_BLOCKED", exit_code=EXIT_SAFETY_BLOCK)

    plans = plan_worktrees(root, task, git=git)
    if dry_run:
        return plans

    created = create_worktrees(plans, git=git)
    executions = {
        p.repo_id: RepoExecution(branch=p.branch, baseline=p.baseline, baseline_commit=p.baseline_commit)
        for p in created
    }
    updated = replace(task, status="active", started_at=_now_iso(), executions=executions)
    save_task(root, updated)
    return created


def _observe_task_worktrees(
    root: Path,
    task: Task,
    git: GitClient,
    *,
    refresh_remote: bool = False,
) -> dict[str, RepoWorktreeObservation]:
    obs: dict[str, RepoWorktreeObservation] = {}
    if task.status != "active":
        return obs
    try:
        local = load_local_config(root)
    except AmwLiteError:
        local = None
    for repo_id in task.repositories:
        exec_info = task.executions.get(repo_id)
        if not exec_info or local is None:
            obs[repo_id] = RepoWorktreeObservation(repo_id=repo_id, exists=False)
            continue
        wt = task_worktree_path(root, task.requirement_id, task.task_id, repo_id)
        if not wt.is_dir():
            obs[repo_id] = RepoWorktreeObservation(repo_id=repo_id, exists=False)
            continue
        clean = not git.is_dirty(wt)
        branch = git.current_branch(wt)
        commit = git.current_head(wt)
        merged: bool | None = None
        if refresh_remote:
            if git.remote_url(wt, "origin"):
                remote_head = git.fetch_branch_head(wt, "origin", exec_info.baseline)
            else:
                remote_head = git.resolve_commit(wt, f"refs/heads/{exec_info.baseline}")
            merged = git.is_ancestor(wt, commit, remote_head)
        obs[repo_id] = RepoWorktreeObservation(
            repo_id=repo_id,
            exists=True,
            clean=clean,
            branch=branch,
            commit=commit,
            merged_to_baseline=merged,
        )
    return obs


def build_task_mr_plans(root: Path, req_id: str, task_id: str, *, git: GitClient | None = None) -> tuple[MrPlan, ...]:
    """Validate check + human Code Review at current HEAD and emit explicit MR handoff details."""
    client = git or GitClient()
    task = load_task(root, req_id, task_id)
    if task.status != "active":
        raise AmwLiteError(f"task mr requires active status (current: {task.status})", code="TASK_NOT_ACTIVE", exit_code=EXIT_SAFETY_BLOCK)
    repos_by_id = {r.repo_id: r for r in load_repositories(root)}
    obs = _observe_task_worktrees(root, task, client, refresh_remote=False)

    commits: dict[str, str] = {}
    for repo_id in task.repositories:
        o = obs[repo_id]
        exec_info = task.executions.get(repo_id)
        if not exec_info or not o.exists:
            raise AmwLiteError(f"worktree missing for {repo_id}", code="WORKTREE_MISSING", exit_code=EXIT_SAFETY_BLOCK)
        if not o.clean:
            raise AmwLiteError(f"dirty worktree blocks MR handoff for {repo_id}", code="WORKTREE_DIRTY", exit_code=EXIT_SAFETY_BLOCK)
        if o.branch != exec_info.branch:
            raise AmwLiteError(f"branch drift blocks MR handoff for {repo_id}", code="BRANCH_DRIFT", exit_code=EXIT_SAFETY_BLOCK)
        commits[repo_id] = o.commit

    commands = effective_verify_commands(task, repos_by_id)
    if not checks_pass_for_commits(task, commands, commits):
        raise AmwLiteError("task check evidence missing or stale for current HEAD", code="CHECK_REQUIRED", exit_code=EXIT_SAFETY_BLOCK)
    _, approved = review_state_for_commits(task, commits)
    if not approved:
        raise AmwLiteError(
            f"human Code Review approval required at current HEAD: amw-lite task review {req_id} {task_id} --approve",
            code="REVIEW_APPROVAL_REQUIRED",
            exit_code=EXIT_SAFETY_BLOCK,
        )

    plans: list[MrPlan] = []
    for repo_id in task.repositories:
        exec_info = task.executions[repo_id]
        repo_cfg = repos_by_id[repo_id]
        url = build_mr_url(repo_cfg.remote, exec_info.branch, exec_info.baseline) if repo_cfg.remote else ""
        wt = task_worktree_path(root, req_id, task_id, repo_id)
        plans.append(
            MrPlan(
                repo_id=repo_id,
                source_branch=exec_info.branch,
                target_branch=exec_info.baseline,
                commit=commits[repo_id],
                url=url,
                push_command=f"git -C {wt} push -u origin {exec_info.branch}",
            )
        )
    return tuple(plans)


def finish_task(root: Path, req_id: str, task_id: str, *, git: GitClient | None = None) -> Task:
    """Verify check, review approval, and remote baseline merge ancestry before marking Task done."""
    client = git or GitClient()
    task = load_task(root, req_id, task_id)
    if task.status == "done":
        return task
    # Reuse strict local check + review approval gates first
    build_task_mr_plans(root, req_id, task_id, git=client)
    obs = _observe_task_worktrees(root, task, client, refresh_remote=True)
    unmerged = [r for r in task.repositories if not obs[r].merged_to_baseline]
    if unmerged:
        raise AmwLiteError(
            f"task commits are not yet merged into remote baseline for: {', '.join(unmerged)}",
            code="NOT_MERGED",
            exit_code=EXIT_SAFETY_BLOCK,
        )
    updated = replace(task, status="done")
    save_task(root, updated)
    return updated


def generate_release_plan(root: Path, req_id: str) -> Path:
    """Generate/refresh `release-plan.md` from topological Task order and verified commits."""
    req = load_requirement(root, req_id)
    tasks = {t.task_id: t for t in list_tasks(root, req_id) if t.status != "cancelled"}
    if not tasks:
        raise AmwLiteError(f"requirement {req_id} has no active/completed Tasks", code="NO_TASKS", exit_code=EXIT_VALIDATION_ERROR)
    pending = [tid for tid, t in tasks.items() if t.status != "done"]
    if pending:
        raise AmwLiteError(
            f"all Tasks must be done (merged) before generating final release plan; pending: {', '.join(sorted(pending))}",
            code="TASKS_NOT_DONE",
            exit_code=EXIT_SAFETY_BLOCK,
        )
    if not req.gates.e2e_result_approved:
        raise AmwLiteError(
            f"E2E results must be approved before release plan: amw-lite req approve {req_id} --gate e2e-result",
            code="E2E_NOT_APPROVED",
            exit_code=EXIT_SAFETY_BLOCK,
        )

    order = topological_task_order(tasks)
    lines = [
        f"# Release Plan: {req.requirement_id} — {req.title}",
        "",
        f"- Generated at: {_now_iso()}",
        f"- Kind: `{req.kind}`",
        "- E2E Status: Approved (Gate #5)",
        "",
        "## Deployment Order & Verified Commits",
        "",
    ]
    for idx, tid in enumerate(order, 1):
        t = tasks[tid]
        lines.append(f"### {idx}. Task `{t.task_id}` — {t.title}")
        latest_reviews = {r.repo_id: r for r in t.reviews if r.human_approved}
        for repo_id in t.repositories:
            exec_info = t.executions.get(repo_id)
            rev = latest_reviews.get(repo_id)
            commit = rev.commit if rev else (exec_info.baseline_commit if exec_info else "unknown")
            baseline = exec_info.baseline if exec_info else "main"
            lines.append(f"- `{repo_id}`: target `{baseline}`, verified commit `{commit}`")
        lines.append("")

    lines.extend(
        [
            "## Rollback & Verification Notes",
            "- Rollback: revert the merged baseline commits in reverse dependency order.",
            "- Post-deploy verification: execute smoke checks from `e2e-plan.md`.",
            "",
            "## Human Approval (Gate #6)",
            f"- Confirm plan via: `amw-lite req approve {req.requirement_id} --gate release-plan`",
            "- PROD deployment is manual-only; record actual release time and operator below.",
            "",
        ]
    )
    out = requirement_dir(root, req_id) / "release-plan.md"
    atomic_write_text(out, "\n".join(lines))
    return out


def close_requirement(root: Path, req_id: str, *, git: GitClient | None = None) -> dict[str, Any]:
    """Close Requirement after all Tasks done + E2E & Release gates approved; emit knowledge & cleanup reminders."""
    req = load_requirement(root, req_id)
    tasks = [t for t in list_tasks(root, req_id) if t.status != "cancelled"]
    pending = [t.task_id for t in tasks if t.status != "done"]
    if not tasks or pending:
        raise AmwLiteError(
            f"cannot close requirement while Tasks are unfinished: {', '.join(pending) or 'no tasks'}",
            code="TASKS_NOT_DONE",
            exit_code=EXIT_SAFETY_BLOCK,
        )
    if not req.gates.e2e_result_approved:
        raise AmwLiteError("e2e-result gate must be approved before closing requirement", code="E2E_NOT_APPROVED", exit_code=EXIT_SAFETY_BLOCK)
    if not req.gates.release_plan_approved:
        raise AmwLiteError("release-plan gate must be approved before closing requirement", code="RELEASE_PLAN_NOT_APPROVED", exit_code=EXIT_SAFETY_BLOCK)

    updated = replace(req, status="closed")
    save_requirement(root, updated)

    remaining_cleanups: list[str] = []
    for t in tasks:
        try:
            items = plan_cleanup_worktrees(root, t, git=git)
            if items:
                remaining_cleanups.append(f"amw-lite task cleanup {req_id} {t.task_id} --apply")
        except AmwLiteError:
            remaining_cleanups.append(f"amw-lite task cleanup {req_id} {t.task_id}  # inspect dirty/drifted worktrees")

    return {
        "requirement_id": req_id,
        "status": "closed",
        "knowledge_prompt": "Distill 1-3 durable learnings from design vs implementation into context/ (user picks which to keep or skip).",
        "cleanup_suggestions": remaining_cleanups,
    }


def build_resume_view(
    root: Path,
    *,
    req_id: str | None = None,
    task_id: str | None = None,
    refresh_remote: bool = False,
    git: GitClient | None = None,
) -> dict[str, Any]:
    """Build deterministic resume snapshot and single NEXT recommendation."""
    ws = load_workspace(root)
    repos_by_id = {r.repo_id: r for r in load_repositories(root)}
    client = git or GitClient()

    reqs = list_requirements(root)
    if req_id:
        req = load_requirement(root, req_id)
    else:
        open_reqs = [r for r in reqs if r.status == "open"]
        if not open_reqs:
            return {
                "workspace_id": ws.workspace_id,
                "requirement_id": None,
                "next": {
                    "action": "req.new",
                    "command": "amw-lite req new REQ-001 --title \"<requirement title>\"",
                    "reason": "No open Requirement found. Create a new Requirement to begin.",
                    "requires_human_gate": None,
                    "alternatives": [],
                },
            }
        req = open_reqs[0]

    tasks = list_tasks(root, req.requirement_id)
    tasks_by_id = {t.task_id: t for t in tasks}
    stage_results: dict[str, TaskStageResult] = {}
    task_summaries: list[dict[str, Any]] = []

    for t in tasks:
        obs = _observe_task_worktrees(root, t, client, refresh_remote=refresh_remote)
        plan_err = check_markdown_complete(task_dir(root, req.requirement_id, t.task_id) / "plan.md") if t.status == "draft" else None
        sr = derive_task_stage(
            t,
            tasks_by_id,
            repos_by_id,
            lambda c: read_contract_status(root, c),
            obs,
            plan_error=plan_err,
        )
        stage_results[t.task_id] = sr
        task_summaries.append(
            {
                "task_id": t.task_id,
                "title": t.title,
                "status": t.status,
                "effective_stage": sr.stage.value,
                "repositories": list(t.repositories),
                "blockers": list(sr.blockers),
                "worktrees": {
                    rid: {
                        "exists": o.exists,
                        "clean": o.clean,
                        "branch": o.branch,
                        "commit": o.commit[:10] if o.commit else "",
                        "merged": o.merged_to_baseline,
                    }
                    for rid, o in obs.items()
                },
            }
        )

    nxt = derive_next_action(req, tasks, stage_results, focus_task_id=task_id)
    return {
        "workspace_id": ws.workspace_id,
        "requirement_id": req.requirement_id,
        "requirement_title": req.title,
        "requirement_status": req.status,
        "gates": req.gates.to_dict(),
        "tasks": task_summaries,
        "next": nxt.to_dict(),
    }
