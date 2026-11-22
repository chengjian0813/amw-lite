"""Tests for Commit-bound Check reuse/invalidation, Fake Alibaba `ocr` hook, fallback, E2E, and PROD block."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from amw_lite.check import run_task_check
from amw_lite.config import atomic_write_text, dump_yaml, load_task, load_workspace, load_yaml_file
from amw_lite.errors import EXIT_SAFETY_BLOCK, AmwLiteError
from amw_lite.hooks import run_e2e_hook, run_release_hook, run_task_review
from amw_lite.workspace_ops import approve_requirement_gate, create_requirement, create_task, start_task
from amw_lite.worktree import task_worktree_path
from tests.conftest import MultiRepoEnv, git_cmd


def _write_executable(path: Path, body: str) -> Path:
    atomic_write_text(path, body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _setup_active_task(env: MultiRepoEnv) -> tuple[Path, Path]:
    ws = env.workspace
    create_requirement(ws, "REQ-002", "Payment retry")
    atomic_write_text(ws / "requirements/REQ-002/brief.md", "# Brief\nAdd idempotent retry.\n")
    atomic_write_text(ws / "requirements/REQ-002/design.md", "# Design\nCore handles retry token.\n")
    approve_requirement_gate(ws, "REQ-002", "brief")
    approve_requirement_gate(ws, "REQ-002", "design")
    create_task(ws, "REQ-002", "T01", "Retry impl", ("order-core", "order-biz"))
    atomic_write_text(ws / "requirements/REQ-002/tasks/T01/plan.md", "# Plan\n1. Add retry logic.\n")
    start_task(ws, "REQ-002", "T01")
    return (
        task_worktree_path(ws, "REQ-002", "T01", "order-core"),
        task_worktree_path(ws, "REQ-002", "T01", "order-biz"),
    )


def test_check_parallel_reuse_and_invalidation_on_fix(multi_repo_env: MultiRepoEnv) -> None:
    core_wt, _ = _setup_active_task(multi_repo_env)
    ws = multi_repo_env.workspace

    first = run_task_check(ws, load_task(ws, "REQ-002", "T01"))
    assert first.passed
    assert all(not o.reused for o in first.outcomes)

    # Re-running at identical clean commit reuses both repos
    second = run_task_check(ws, load_task(ws, "REQ-002", "T01"))
    assert second.passed
    assert all(o.reused for o in second.outcomes)

    # Fix a file in order-core only -> order-core reruns, order-biz stays REUSE!
    atomic_write_text(core_wt / "retry.txt", "fix\n")
    git_cmd(core_wt, "add", "retry.txt")
    git_cmd(core_wt, "commit", "-m", "fix: retry edge case")
    third = run_task_check(ws, load_task(ws, "REQ-002", "T01"))
    by_repo = {o.repo_id: o for o in third.outcomes}
    assert by_repo["order-core"].reused is False
    assert by_repo["order-biz"].reused is True


def test_review_with_fake_ocr_and_fallback_and_in_branch_reapprove(
    multi_repo_env: MultiRepoEnv, tmp_path: Path
) -> None:
    core_wt, _ = _setup_active_task(multi_repo_env)
    ws = multi_repo_env.workspace
    run_task_check(ws, load_task(ws, "REQ-002", "T01"))

    # Create fake `ocr` CLI script and wire via local.yaml hooks_override
    fake_ocr = _write_executable(
        tmp_path / "fake-ocr",
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "assert 'review' in sys.argv and '--from' in sys.argv and '--to' in sys.argv\n"
        "print(json.dumps({'findings': [{'file': 'retry.txt', 'line': 1, 'message': 'check timeout'}]}))\n",
    )
    local_raw = load_yaml_file(ws / ".workspace/local.yaml")
    local_raw["hooks_override"] = {
        "review": [
            str(fake_ocr),
            "review",
            "--from",
            "{baseline_commit}",
            "--to",
            "{head_commit}",
            "--background",
            "{review_background}",
            "--format",
            "json",
        ]
    }
    atomic_write_text(ws / ".workspace/local.yaml", dump_yaml(local_raw))

    # Cannot approve before running review
    with pytest.raises(AmwLiteError, match="before --approve"):
        run_task_review(ws, load_task(ws, "REQ-002", "T01"), approve=True)

    # Run fake ocr review
    reviews = run_task_review(ws, load_task(ws, "REQ-002", "T01"))
    assert len(reviews) == 2
    assert all(r.tool == "fake-ocr" and r.findings_count == 1 and not r.human_approved for r in reviews)

    # Fix CR issue directly in the same branch -> commit -> re-check -> re-review -> approve
    atomic_write_text(core_wt / "retry.txt", "timeout=3000\n")
    git_cmd(core_wt, "add", "retry.txt")
    git_cmd(core_wt, "commit", "-m", "fix: address ocr timeout comment")
    run_task_check(ws, load_task(ws, "REQ-002", "T01"))
    run_task_review(ws, load_task(ws, "REQ-002", "T01"))
    approved = run_task_review(ws, load_task(ws, "REQ-002", "T01"), approve=True)
    assert all(r.human_approved for r in approved)

    # Verify fallback to diff-stat when review hook disabled
    ws_raw = load_workspace(ws).to_dict()
    ws_raw["hooks"]["review"]["enabled"] = False
    atomic_write_text(ws / ".workspace/workspace.yaml", dump_yaml(ws_raw))
    local_raw["hooks_override"] = {}
    atomic_write_text(ws / ".workspace/local.yaml", dump_yaml(local_raw))
    fallback = run_task_review(ws, load_task(ws, "REQ-002", "T01"))
    assert all(r.tool == "diff-stat" for r in fallback)


def test_e2e_hook_in_branch_and_prod_release_hard_blocked(
    multi_repo_env: MultiRepoEnv, tmp_path: Path
) -> None:
    _setup_active_task(multi_repo_env)
    ws = multi_repo_env.workspace
    atomic_write_text(ws / "requirements/REQ-002/e2e-plan.md", "# E2E\n- [ ] Verify retry idempotency\n")
    approve_requirement_gate(ws, "REQ-002", "e2e-plan")

    fake_e2e = _write_executable(
        tmp_path / "fake-e2e",
        f"#!{sys.executable}\nimport sys\nprint('E2E OK', sys.argv[1:])\n",
    )
    local_raw = load_yaml_file(ws / ".workspace/local.yaml")
    local_raw["hooks_override"] = {"e2e": [str(fake_e2e), "{req_id}", "{e2e_plan_path}"]}
    atomic_write_text(ws / ".workspace/local.yaml", dump_yaml(local_raw))

    # Preview then apply E2E while Task is still active (in-branch E2E supported!)
    prev = run_e2e_hook(ws, "REQ-002", apply=False)
    assert not prev.applied and prev.argv[0] == str(fake_e2e)
    applied = run_e2e_hook(ws, "REQ-002", apply=True)
    assert applied.applied and applied.exit_code == 0

    # PROD deployment must always fail closed with EXIT_SAFETY_BLOCK
    with pytest.raises(AmwLiteError) as exc_info:
        run_release_hook(ws, "REQ-002", "prod", apply=True)
    assert exc_info.value.exit_code == EXIT_SAFETY_BLOCK
    assert exc_info.value.code == "PROD_AUTO_DEPLOY_FORBIDDEN"
