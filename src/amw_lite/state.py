"""Pure domain state derivation: dependency DAG, contract gates, EffectiveStage, and NEXT."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from amw_lite.errors import EXIT_VALIDATION_ERROR, AmwLiteError
from amw_lite.models import RepositoryConfig, Requirement, Task, VerifyCommand


class EffectiveStage(str, Enum):
    """Derived runtime stage of a Task (never persisted separately)."""

    BLOCKED = "BLOCKED"
    READY = "READY"
    DEVELOPING = "DEVELOPING"
    CHECKED = "CHECKED"
    REVIEWED = "REVIEWED"
    MERGED = "MERGED"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class RepoWorktreeObservation:
    """Observed local Git state for one repository worktree of a Task."""

    repo_id: str
    exists: bool
    clean: bool = False
    branch: str = ""
    commit: str = ""
    merged_to_baseline: bool | None = None


@dataclass(frozen=True)
class TaskStageResult:
    """Derived stage and blockers for one Task."""

    task_id: str
    stage: EffectiveStage
    blockers: tuple[str, ...] = ()
    review_pending_approval: bool = False


@dataclass(frozen=True)
class NextAction:
    """Single recommended action returned by `amw-lite resume`."""

    action: str
    command: str
    reason: str
    requires_human_gate: str | None = None
    alternatives: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "command": self.command,
            "reason": self.reason,
            "requires_human_gate": self.requires_human_gate,
            "alternatives": list(self.alternatives),
        }


def effective_verify_commands(task: Task, repos_by_id: dict[str, RepositoryConfig]) -> tuple[VerifyCommand, ...]:
    """Resolve effective verify commands per repo, falling back to repositories.yaml defaults."""
    if task.verify_commands:
        return task.verify_commands
    resolved: list[VerifyCommand] = []
    for repo_id in task.repositories:
        repo = repos_by_id.get(repo_id)
        if repo:
            for cmd in repo.default_verify:
                resolved.append(VerifyCommand(repo_id=repo_id, command=cmd))
    return tuple(resolved)


def detect_dependency_cycle(tasks: dict[str, Task]) -> list[str] | None:
    """Return full cycle path if task dependencies contain a cycle, else None."""
    state: dict[str, int] = {}  # 0/unvisited, 1=visiting, 2=done
    stack: list[str] = []

    def dfs(tid: str) -> list[str] | None:
        state[tid] = 1
        stack.append(tid)
        task = tasks.get(tid)
        if task:
            for dep in task.depends_on:
                nxt = dep.task_id
                if nxt not in tasks:
                    continue
                if state.get(nxt) == 1:
                    idx = stack.index(nxt)
                    return stack[idx:] + [nxt]
                if state.get(nxt, 0) == 0:
                    cycle = dfs(nxt)
                    if cycle:
                        return cycle
        stack.pop()
        state[tid] = 2
        return None

    for tid in sorted(tasks):
        if state.get(tid, 0) == 0:
            found = dfs(tid)
            if found:
                return found
    return None


def topological_task_order(tasks: dict[str, Task]) -> tuple[str, ...]:
    """Return deterministic topological order (upstream first), raising on cycles."""
    cycle = detect_dependency_cycle(tasks)
    if cycle:
        raise AmwLiteError(
            f"task dependency cycle detected: {' -> '.join(cycle)}",
            code="DEPENDENCY_CYCLE",
            exit_code=EXIT_VALIDATION_ERROR,
        )
    pending = set(tasks)
    ordered: list[str] = []
    while pending:
        ready = sorted(
            tid
            for tid in pending
            if not any(d.task_id in pending for d in tasks[tid].depends_on)
        )
        for tid in ready:
            ordered.append(tid)
            pending.remove(tid)
    return tuple(ordered)


def evaluate_ready_blockers(
    task: Task,
    all_tasks: dict[str, Task],
    known_repos: set[str],
    contract_status_lookup: Callable[[str], str | None],
    plan_error: str | None = None,
) -> tuple[str, ...]:
    """Evaluate all blocking conditions preventing a draft Task from starting."""
    blockers: list[str] = []
    if plan_error:
        blockers.append(f"PLAN_INCOMPLETE: {plan_error}")
    for repo_id in task.repositories:
        if repo_id not in known_repos:
            blockers.append(f"UNKNOWN_REPOSITORY: {repo_id}")
    cycle = detect_dependency_cycle(all_tasks)
    if cycle and task.task_id in cycle:
        blockers.append(f"DEPENDENCY_CYCLE: {' -> '.join(cycle)}")
    for contract in task.contracts:
        status = contract_status_lookup(contract)
        if status is None:
            blockers.append(f"CONTRACT_MISSING: {contract}")
        elif status != "accepted":
            blockers.append(f"CONTRACT_NOT_ACCEPTED: {contract} (status={status})")
    for dep in task.depends_on:
        upstream = all_tasks.get(dep.task_id)
        if upstream is None:
            blockers.append(f"DEPENDENCY_MISSING: {dep.task_id}")
            continue
        if dep.dep_type == "hard_code":
            if upstream.status != "done":
                blockers.append(
                    f"HARD_DEPENDENCY_NOT_DONE: {dep.task_id} is {upstream.status} (must merge to baseline first)"
                )
        elif dep.dep_type == "contract":
            c_path = dep.contract or ""
            c_status = contract_status_lookup(c_path)
            if c_status != "accepted":
                blockers.append(
                    f"CONTRACT_DEPENDENCY_NOT_ACCEPTED: {dep.task_id} requires accepted {c_path} (status={c_status or 'missing'})"
                )
    return tuple(blockers)


def checks_pass_for_commits(
    task: Task,
    commands: tuple[VerifyCommand, ...],
    commits_by_repo: dict[str, str],
) -> bool:
    """Return True if every effective verify command has a trusted passing evidence at current HEAD."""
    passing = {
        (ev.repo_id, ev.command, ev.commit)
        for ev in task.checks
        if ev.is_trusted_pass
    }
    for cmd in commands:
        commit = commits_by_repo.get(cmd.repo_id)
        if not commit or (cmd.repo_id, cmd.command, commit) not in passing:
            return False
    return True


def review_state_for_commits(task: Task, commits_by_repo: dict[str, str]) -> tuple[bool, bool]:
    """Return (has_current_review_for_all_repos, all_human_approved_at_current_head)."""
    latest_by_repo_commit = {(r.repo_id, r.commit): r for r in task.reviews}
    has_all = True
    all_approved = True
    for repo_id in task.repositories:
        commit = commits_by_repo.get(repo_id, "")
        rec = latest_by_repo_commit.get((repo_id, commit))
        if rec is None:
            has_all = False
            all_approved = False
        elif not rec.human_approved:
            all_approved = False
    return has_all, all_approved


def derive_task_stage(
    task: Task,
    all_tasks: dict[str, Task],
    repos_by_id: dict[str, RepositoryConfig],
    contract_status_lookup: Callable[[str], str | None],
    observations: dict[str, RepoWorktreeObservation],
    plan_error: str | None = None,
) -> TaskStageResult:
    """Derive the current EffectiveStage purely from task.yaml + objective observations."""
    if task.status == "done":
        return TaskStageResult(task.task_id, EffectiveStage.DONE)
    if task.status == "cancelled":
        return TaskStageResult(task.task_id, EffectiveStage.CANCELLED)

    if task.status == "draft":
        blockers = evaluate_ready_blockers(
            task, all_tasks, set(repos_by_id), contract_status_lookup, plan_error=plan_error
        )
        if blockers:
            return TaskStageResult(task.task_id, EffectiveStage.BLOCKED, blockers)
        return TaskStageResult(task.task_id, EffectiveStage.READY)

    # status == "active"
    issues: list[str] = []
    commits_by_repo: dict[str, str] = {}
    for repo_id in task.repositories:
        obs = observations.get(repo_id)
        exec_info = task.executions.get(repo_id)
        if not exec_info:
            issues.append(f"EXECUTION_MISSING: {repo_id}")
            continue
        if not obs or not obs.exists:
            issues.append(f"WORKTREE_MISSING: {repo_id}")
            continue
        if not obs.clean:
            issues.append(f"WORKTREE_DIRTY: {repo_id}")
            continue
        if obs.branch != exec_info.branch:
            issues.append(f"BRANCH_DRIFT: {repo_id} ({obs.branch} != {exec_info.branch})")
            continue
        commits_by_repo[repo_id] = obs.commit

    if issues:
        return TaskStageResult(task.task_id, EffectiveStage.DEVELOPING, tuple(issues))

    commands = effective_verify_commands(task, repos_by_id)
    if not checks_pass_for_commits(task, commands, commits_by_repo):
        return TaskStageResult(task.task_id, EffectiveStage.DEVELOPING)

    has_review, approved = review_state_for_commits(task, commits_by_repo)
    if not approved:
        return TaskStageResult(
            task.task_id,
            EffectiveStage.CHECKED,
            review_pending_approval=has_review,
        )

    merged_flags = [observations[r].merged_to_baseline for r in task.repositories]
    if merged_flags and all(flag is True for flag in merged_flags):
        return TaskStageResult(task.task_id, EffectiveStage.MERGED)
    return TaskStageResult(task.task_id, EffectiveStage.REVIEWED)


def derive_next_action(
    req: Requirement,
    tasks: tuple[Task, ...],
    stage_results: dict[str, TaskStageResult],
    focus_task_id: str | None = None,
) -> NextAction:
    """Derive a single unambiguous NEXT action aligned with the 6 human sign-off gates."""
    rid = req.requirement_id
    if req.status == "closed":
        return NextAction("none", "", f"Requirement {rid} is already closed.")
    if req.status == "cancelled":
        return NextAction("none", "", f"Requirement {rid} is cancelled.")

    if not req.gates.brief_approved:
        return NextAction(
            action="req.approve.brief",
            command=f"amw-lite req approve {rid} --gate brief",
            reason="Draft/review brief.md and obtain human sign-off (Gate #1: Requirement understanding).",
            requires_human_gate="brief",
        )
    if not req.gates.design_approved:
        return NextAction(
            action="req.approve.design",
            command=f"amw-lite req approve {rid} --gate design",
            reason="Draft/review design.md and cross-repo contracts, then obtain human sign-off (Gate #2: System design).",
            requires_human_gate="design",
        )

    non_cancelled = [t for t in tasks if t.status != "cancelled"]
    if not non_cancelled:
        return NextAction(
            action="task.new",
            command=f"amw-lite task new {rid} T01 --repos <repo_id> --title \"<task title>\"",
            reason="Split the approved design into at least one actionable Task.",
        )

    # Focus an explicit task or pick the first active -> ready -> blocked task
    active_tasks = [t for t in non_cancelled if t.status == "active"]
    if focus_task_id:
        target = next((t for t in non_cancelled if t.task_id == focus_task_id), None)
    elif active_tasks:
        target = active_tasks[0]
    else:
        ready = [t for t in non_cancelled if stage_results[t.task_id].stage == EffectiveStage.READY]
        blocked = [t for t in non_cancelled if stage_results[t.task_id].stage == EffectiveStage.BLOCKED]
        target = ready[0] if ready else (blocked[0] if blocked else None)

    if target is not None and target.status != "done":
        sr = stage_results[target.task_id]
        tid = target.task_id
        if sr.stage == EffectiveStage.BLOCKED:
            return NextAction(
                action="task.unblock",
                command=f"amw-lite resume --req {rid} --task {tid}",
                reason=f"Task {tid} is blocked: {'; '.join(sr.blockers)}",
            )
        if sr.stage == EffectiveStage.READY:
            return NextAction(
                action="task.start",
                command=f"amw-lite task start {rid} {tid}",
                reason=f"Task {tid} passed all planning/contract/dependency gates and is ready to start.",
            )
        if sr.stage == EffectiveStage.DEVELOPING:
            if sr.blockers:
                return NextAction(
                    action="task.fix_worktree",
                    command=f"amw-lite task check {rid} {tid}",
                    reason=f"Commit or resolve worktree state ({'; '.join(sr.blockers)}), then run check.",
                )
            return NextAction(
                action="task.check",
                command=f"amw-lite task check {rid} {tid}",
                reason=f"Run commit-bound verification commands for Task {tid}.",
            )
        if sr.stage == EffectiveStage.CHECKED:
            if sr.review_pending_approval:
                return NextAction(
                    action="task.review.approve",
                    command=f"amw-lite task review {rid} {tid} --approve",
                    reason=f"Inspect Code Review report for Task {tid}; fix in current branch if needed, or approve (Gate #3: Code Review).",
                    requires_human_gate="code_review",
                )
            return NextAction(
                action="task.review",
                command=f"amw-lite task review {rid} {tid}",
                reason=f"All checks passed for Task {tid}; run Code Review (ocr) for human review (Gate #3).",
                requires_human_gate="code_review",
            )
        if sr.stage == EffectiveStage.REVIEWED:
            # Default in-branch loop: run E2E before MR when E2E isn't approved yet,
            # while offering `task mr` as an explicit alternative for multi-task integration E2E.
            if not req.gates.e2e_plan_approved:
                return NextAction(
                    action="req.approve.e2e_plan",
                    command=f"amw-lite req approve {rid} --gate e2e-plan",
                    reason="Review and approve e2e-plan.md before running E2E in current branch (Gate #4: E2E case design).",
                    requires_human_gate="e2e_plan",
                    alternatives=(f"amw-lite task mr {rid} {tid}",),
                )
            if not req.gates.e2e_result_approved:
                return NextAction(
                    action="e2e.run_or_approve",
                    command=f"amw-lite e2e run {rid}",
                    reason="Execute E2E tests (fix bugs directly in current branch), then approve results (Gate #5: E2E result).",
                    requires_human_gate="e2e_result",
                    alternatives=(
                        f"amw-lite req approve {rid} --gate e2e-result",
                        f"amw-lite task mr {rid} {tid}",
                    ),
                )
            return NextAction(
                action="task.mr",
                command=f"amw-lite task mr {rid} {tid}",
                reason=f"Task {tid} passed check, review, and E2E; push branch and create/merge MR, then run finish.",
                alternatives=(f"amw-lite task finish {rid} {tid}",),
            )
        if sr.stage == EffectiveStage.MERGED:
            return NextAction(
                action="task.finish",
                command=f"amw-lite task finish {rid} {tid}",
                reason=f"All repositories for Task {tid} are merged into baseline; finalize Task.",
            )

    # All non-cancelled tasks are done -> E2E (if post-merge path) -> Release -> Close
    if not req.gates.e2e_plan_approved:
        return NextAction(
            action="req.approve.e2e_plan",
            command=f"amw-lite req approve {rid} --gate e2e-plan",
            reason="All Tasks are merged; review and approve e2e-plan.md (Gate #4: E2E case design).",
            requires_human_gate="e2e_plan",
        )
    if not req.gates.e2e_result_approved:
        return NextAction(
            action="e2e.run_or_approve",
            command=f"amw-lite e2e run {rid}",
            reason="Execute integration E2E tests and approve results (Gate #5: E2E result).",
            requires_human_gate="e2e_result",
            alternatives=(f"amw-lite req approve {rid} --gate e2e-result",),
        )
    if not req.gates.release_plan_approved:
        return NextAction(
            action="release.plan",
            command=f"amw-lite release plan {rid}",
            reason="Generate/review release-plan.md and approve release strategy (Gate #6: Release plan).",
            requires_human_gate="release_plan",
            alternatives=(f"amw-lite req approve {rid} --gate release-plan",),
        )
    return NextAction(
        action="req.close",
        command=f"amw-lite req close {rid}",
        reason="Review 1-3 distilled knowledge candidates for context/, then close the requirement and cleanup worktrees.",
        alternatives=(f"amw-lite release run {rid} --env qa",),
    )
