"""Real local multi-repository Git Worktree integration tests."""

from __future__ import annotations

import pytest

from amw_lite.config import atomic_write_text, load_task, save_task
from amw_lite.errors import AmwLiteError
from amw_lite.git import GitClient
from amw_lite.workspace_ops import approve_requirement_gate, create_requirement, create_task, start_task
from amw_lite.worktree import apply_cleanup_worktrees, plan_cleanup_worktrees, task_worktree_path
from tests.conftest import MultiRepoEnv, git_cmd


def _prepare_ready_task(env: MultiRepoEnv, task_id: str = "T01") -> None:
    ws = env.workspace
    create_requirement(ws, "REQ-001", "Checkout flow")
    atomic_write_text(ws / "requirements/REQ-001/brief.md", "# Brief\nImplement checkout.\n")
    atomic_write_text(ws / "requirements/REQ-001/design.md", "# Design\nCore + Biz changes.\n")
    approve_requirement_gate(ws, "REQ-001", "brief")
    approve_requirement_gate(ws, "REQ-001", "design")
    create_task(ws, "REQ-001", task_id, "Checkout task", ("order-core", "order-biz"))
    atomic_write_text(ws / f"requirements/REQ-001/tasks/{task_id}/plan.md", "# Plan\n1. Update core and biz.\n")


def test_multi_repo_worktree_start_and_safe_cleanup(multi_repo_env: MultiRepoEnv) -> None:
    _prepare_ready_task(multi_repo_env)
    ws = multi_repo_env.workspace
    git = GitClient()

    # Dry-run creates nothing
    preview = start_task(ws, "REQ-001", "T01", dry_run=True)
    assert len(preview) == 2
    assert not task_worktree_path(ws, "REQ-001", "T01", "order-core").exists()

    # Actual start creates isolated worktrees in both business repos
    created = start_task(ws, "REQ-001", "T01", dry_run=False)
    assert len(created) == 2
    core_wt = task_worktree_path(ws, "REQ-001", "T01", "order-core")
    biz_wt = task_worktree_path(ws, "REQ-001", "T01", "order-biz")
    assert core_wt.is_dir() and biz_wt.is_dir()
    assert git.current_branch(core_wt) == "amw/REQ-001/T01"
    assert git.current_branch(biz_wt) == "amw/REQ-001/T01"

    # Mark done to test cleanup safety
    t = load_task(ws, "REQ-001", "T01")
    from dataclasses import replace

    save_task(ws, replace(t, status="done"))

    # Dirty worktree blocks cleanup
    atomic_write_text(core_wt / "untracked.txt", "unsaved work\n")
    with pytest.raises(AmwLiteError, match="dirty worktree blocks cleanup"):
        plan_cleanup_worktrees(ws, load_task(ws, "REQ-001", "T01"))
    (core_wt / "untracked.txt").unlink()

    # Clean worktree removes directory but preserves branch
    items = plan_cleanup_worktrees(ws, load_task(ws, "REQ-001", "T01"))
    assert len(items) == 2
    apply_cleanup_worktrees(items)
    assert not core_wt.exists() and not biz_wt.exists()
    assert git.branch_exists(multi_repo_env.core_repo, "amw/REQ-001/T01")


def test_dirty_source_blocks_start_and_preserves_user_work(multi_repo_env: MultiRepoEnv) -> None:
    _prepare_ready_task(multi_repo_env)
    # Modify a tracked file in source checkout
    atomic_write_text(multi_repo_env.biz_repo / "README.md", "# dirty tracked change\n")
    with pytest.raises(AmwLiteError, match="tracked uncommitted changes"):
        start_task(multi_repo_env.workspace, "REQ-001", "T01")
    assert not task_worktree_path(multi_repo_env.workspace, "REQ-001", "T01", "order-core").exists()
    # Clean source checkout back so fixture remains tidy
    git_cmd(multi_repo_env.biz_repo, "checkout", "--", "README.md")
