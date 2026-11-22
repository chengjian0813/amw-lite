"""Multi-repository parallel Git Worktree creation, rollback, and safe cleanup."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

from amw_lite.config import load_local_config, load_repositories, worktree_base_dir
from amw_lite.errors import EXIT_SAFETY_BLOCK, AmwLiteError
from amw_lite.git import GitClient, remote_identity
from amw_lite.models import Task

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True)
class WorktreePlan:
    """One preflighted business repository worktree plan."""

    repo_id: str
    source: Path
    target: Path
    branch: str
    baseline: str
    baseline_commit: str


@dataclass(frozen=True)
class CleanupItem:
    """One clean registered Task worktree eligible for removal."""

    repo_id: str
    source: Path
    target: Path
    branch: str


def run_grouped_parallel(
    items: list[T],
    group_key: Callable[[T], object],
    worker: Callable[[T], R],
    max_workers: int = 4,
) -> list[tuple[T, R | None, Exception | None]]:
    """Run items in parallel across distinct groups (e.g. git repos), serial within each group."""
    groups: dict[object, list[tuple[int, T]]] = defaultdict(list)
    for idx, item in enumerate(items):
        groups[group_key(item)].append((idx, item))

    results: list[tuple[T, R | None, Exception | None] | None] = [None] * len(items)

    def run_group(group: list[tuple[int, T]]) -> None:
        for idx, item in group:
            try:
                results[idx] = (item, worker(item), None)
            except Exception as exc:
                results[idx] = (item, None, exc)

    if len(groups) <= 1:
        for grp in groups.values():
            run_group(grp)
    else:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(groups))) as pool:
            futures = [pool.submit(run_group, grp) for grp in groups.values()]
            for fut in futures:
                fut.result()

    return [r for r in results if r is not None]


def task_worktree_path(root: Path, req_id: str, task_id: str, repo_id: str) -> Path:
    return worktree_base_dir(root) / req_id / task_id / repo_id


def default_task_branch(req_id: str, task_id: str) -> str:
    return f"amw/{req_id}/{task_id}"


def plan_worktrees(root: Path, task: Task, git: GitClient | None = None) -> tuple[WorktreePlan, ...]:
    """Preflight all business repositories without mutating Git state."""
    client = git or GitClient()
    repos_by_id = {r.repo_id: r for r in load_repositories(root)}
    local = load_local_config(root)

    candidates: list[tuple[str, Path, Path, str, str]] = []
    for repo_id in task.repositories:
        if repo_id not in repos_by_id:
            raise AmwLiteError(f"unknown repo_id: {repo_id}", code="UNKNOWN_REPOSITORY", exit_code=EXIT_SAFETY_BLOCK)
        if repo_id not in local.repositories:
            raise AmwLiteError(f"no local path configured in .workspace/local.yaml for {repo_id}", code="LOCAL_REPO_MISSING", exit_code=EXIT_SAFETY_BLOCK)
        repo_cfg = repos_by_id[repo_id]
        source = Path(local.repositories[repo_id])
        if not source.is_dir():
            raise AmwLiteError(f"source checkout does not exist for {repo_id}: {source}", code="SOURCE_MISSING", exit_code=EXIT_SAFETY_BLOCK)
        if repo_cfg.remote:
            actual = remote_identity(client.remote_url(source, "origin"))
            expected = remote_identity(repo_cfg.remote)
            if actual and actual != expected:
                raise AmwLiteError(
                    f"remote mismatch for {repo_id}: expected {expected}, actual {actual}",
                    code="REMOTE_MISMATCH",
                    exit_code=EXIT_SAFETY_BLOCK,
                )
        if client.has_tracked_changes(source):
            raise AmwLiteError(
                f"tracked uncommitted changes in source checkout block {repo_id}: {source}",
                code="SOURCE_DIRTY",
                exit_code=EXIT_SAFETY_BLOCK,
            )
        branch = default_task_branch(task.requirement_id, task.task_id)
        existing_wt = client.worktree_for_branch(source, branch)
        if existing_wt is not None:
            raise AmwLiteError(f"branch {branch} is already checked out at {existing_wt}", code="BRANCH_EXISTS", exit_code=EXIT_SAFETY_BLOCK)
        if client.branch_exists(source, branch):
            raise AmwLiteError(f"branch already exists for {repo_id}: {branch}", code="BRANCH_EXISTS", exit_code=EXIT_SAFETY_BLOCK)
        target = task_worktree_path(root, task.requirement_id, task.task_id, repo_id)
        if target.exists():
            raise AmwLiteError(f"worktree target already exists: {target}", code="WORKTREE_EXISTS", exit_code=EXIT_SAFETY_BLOCK)
        candidates.append((repo_id, source, target, branch, repo_cfg.baseline))

    def fetch_remote_sha(item: tuple[str, Path, Path, str, str]) -> WorktreePlan:
        repo_id, source, target, branch, baseline = item
        if client.remote_url(source, "origin"):
            sha = client.remote_branch_head(source, "origin", baseline)
        else:
            sha = client.resolve_commit(source, f"refs/heads/{baseline}")
        return WorktreePlan(
            repo_id=repo_id,
            source=source,
            target=target,
            branch=branch,
            baseline=baseline,
            baseline_commit=sha,
        )

    outcomes = run_grouped_parallel(candidates, group_key=lambda x: x[1].resolve(), worker=fetch_remote_sha)
    errors = [err for _, _, err in outcomes if err is not None]
    if errors:
        if len(errors) == 1 and isinstance(errors[0], AmwLiteError):
            raise errors[0]
        raise AmwLiteError(f"worktree planning failed: {'; '.join(str(e) for e in errors)}", code="WORKTREE_PLAN_FAILED", exit_code=EXIT_SAFETY_BLOCK)
    return tuple(val for _, val, _ in outcomes if val is not None)


def create_worktrees(plans: tuple[WorktreePlan, ...], git: GitClient | None = None) -> tuple[WorktreePlan, ...]:
    """Fetch exact baselines and create business worktrees; rollback newly created clean worktrees on partial error."""
    client = git or GitClient()

    def sync_and_add(plan: WorktreePlan) -> WorktreePlan:
        if client.has_tracked_changes(plan.source):
            raise AmwLiteError(f"tracked changes in source checkout block {plan.repo_id}", code="SOURCE_DIRTY", exit_code=EXIT_SAFETY_BLOCK)
        if client.remote_url(plan.source, "origin"):
            commit = client.fetch_branch_head(plan.source, "origin", plan.baseline)
            checked_out = client.worktree_for_branch(plan.source, plan.baseline)
            if checked_out is not None:
                if client.has_tracked_changes(checked_out):
                    raise AmwLiteError(
                        f"tracked changes in baseline checkout block fast-forward for {plan.repo_id}",
                        code="BASELINE_DIRTY",
                        exit_code=EXIT_SAFETY_BLOCK,
                    )
                client.fast_forward_checked_out(checked_out, commit)
            else:
                client.fast_forward_branch_ref(plan.source, plan.baseline, commit)
        else:
            commit = client.resolve_commit(plan.source, f"refs/heads/{plan.baseline}")
        client.worktree_add(plan.source, plan.target, plan.branch, commit)
        return WorktreePlan(
            repo_id=plan.repo_id,
            source=plan.source,
            target=plan.target,
            branch=plan.branch,
            baseline=plan.baseline,
            baseline_commit=commit,
        )

    outcomes = run_grouped_parallel(list(plans), group_key=lambda p: p.source.resolve(), worker=sync_and_add)
    errors = [err for _, _, err in outcomes if err is not None]
    if errors:
        for plan, created, err in reversed(outcomes):
            if err is None and created is not None and created.target.exists():
                try:
                    if not client.is_dirty(created.target):
                        client.worktree_remove(created.source, created.target)
                except AmwLiteError:
                    pass
        if len(errors) == 1 and isinstance(errors[0], AmwLiteError):
            raise errors[0]
        raise AmwLiteError(f"worktree creation failed: {'; '.join(str(e) for e in errors)}", code="WORKTREE_CREATE_FAILED", exit_code=EXIT_SAFETY_BLOCK)
    return tuple(val for _, val, _ in outcomes if val is not None)


def plan_cleanup_worktrees(root: Path, task: Task, git: GitClient | None = None) -> tuple[CleanupItem, ...]:
    """Verify every worktree of a completed/cancelled Task is clean and properly registered before cleanup."""
    if task.status not in ("done", "cancelled"):
        raise AmwLiteError(
            f"task cleanup requires done or cancelled status (current: {task.status})",
            code="CLEANUP_NOT_ALLOWED",
            exit_code=EXIT_SAFETY_BLOCK,
        )
    client = git or GitClient()
    local = load_local_config(root)
    items: list[CleanupItem] = []
    for repo_id in task.repositories:
        exec_info = task.executions.get(repo_id)
        if not exec_info:
            continue
        source_raw = local.repositories.get(repo_id)
        if not source_raw:
            raise AmwLiteError(f"no local path configured for {repo_id}", code="LOCAL_REPO_MISSING", exit_code=EXIT_SAFETY_BLOCK)
        source = Path(source_raw)
        target = task_worktree_path(root, task.requirement_id, task.task_id, repo_id)
        if not target.is_dir():
            continue
        registered = client.worktree_for_branch(source, exec_info.branch)
        if registered is None or registered.resolve() != target.resolve():
            raise AmwLiteError(
                f"worktree registration mismatch for {repo_id}: expected {target}, registered {registered}",
                code="WORKTREE_DRIFT",
                exit_code=EXIT_SAFETY_BLOCK,
            )
        if client.is_dirty(target):
            raise AmwLiteError(
                f"dirty worktree blocks cleanup for {repo_id}: {target}",
                code="WORKTREE_DIRTY",
                exit_code=EXIT_SAFETY_BLOCK,
            )
        items.append(CleanupItem(repo_id=repo_id, source=source, target=target, branch=exec_info.branch))
    return tuple(items)


def apply_cleanup_worktrees(items: tuple[CleanupItem, ...], git: GitClient | None = None) -> None:
    """Remove clean verified worktrees while preserving branches."""
    client = git or GitClient()
    for item in items:
        client.worktree_remove(item.source, item.target)
        # Prune empty parent directories up to task directory safely
        for parent in (item.target.parent, item.target.parent.parent):
            try:
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass
