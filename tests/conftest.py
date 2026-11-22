"""Shared real local Git multi-repository fixtures for amw-lite tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from amw_lite.config import atomic_write_text
from amw_lite.workspace_ops import init_workspace


def git_cmd(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr}")
    return proc.stdout.strip()


def make_repo_with_origin(base: Path, name: str, branch: str = "main") -> tuple[Path, Path]:
    """Create a bare origin repository and a clean local clone with one initial commit."""
    origin = base / f"{name}-origin.git"
    local = base / name
    origin.mkdir(parents=True)
    git_cmd(origin, "init", "--bare", f"--initial-branch={branch}")
    local.mkdir(parents=True)
    git_cmd(local, "init", f"--initial-branch={branch}")
    git_cmd(local, "config", "user.name", "Test User")
    git_cmd(local, "config", "user.email", "test@example.invalid")
    git_cmd(local, "remote", "add", "origin", str(origin))
    atomic_write_text(local / "README.md", f"# {name}\n")
    git_cmd(local, "add", "README.md")
    git_cmd(local, "commit", "-m", "chore: initial commit")
    git_cmd(local, "push", "-u", "origin", branch)
    return local, origin


@dataclass(frozen=True)
class MultiRepoEnv:
    workspace: Path
    core_repo: Path
    core_origin: Path
    biz_repo: Path
    biz_origin: Path


@pytest.fixture()
def multi_repo_env(tmp_path: Path) -> MultiRepoEnv:
    """Create an initialized `order-workspace` with real `order-core` and `order-biz` Git repos."""
    core_repo, core_origin = make_repo_with_origin(tmp_path, "order-core")
    biz_repo, biz_origin = make_repo_with_origin(tmp_path, "order-biz")
    ws = tmp_path / "order-workspace"
    ws.mkdir()
    git_cmd(ws, "init", "--initial-branch=main")
    git_cmd(ws, "config", "user.name", "Test User")
    git_cmd(ws, "config", "user.email", "test@example.invalid")

    init_workspace(
        ws,
        workspace_id="order-domain",
        name="Order Domain Workspace",
        repositories=[
            {
                "repo_id": "order-core",
                "name": "Order Core",
                "baseline": "main",
                "role": "core-domain",
                "remote": "",
                "path": str(core_repo),
                "default_verify": ["python3 -c \"print('core ok')\""],
            },
            {
                "repo_id": "order-biz",
                "name": "Order Biz",
                "baseline": "main",
                "role": "business-orchestration",
                "remote": "",
                "path": str(biz_repo),
                "default_verify": ["python3 -c \"print('biz ok')\""],
            },
        ],
    )
    return MultiRepoEnv(
        workspace=ws,
        core_repo=core_repo,
        core_origin=core_origin,
        biz_repo=biz_repo,
        biz_origin=biz_origin,
    )
