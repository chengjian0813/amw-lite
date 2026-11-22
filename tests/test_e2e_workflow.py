"""End-to-end CLI workflow test covering the full 6-gate lifecycle with in-branch CR/E2E fixes."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

from amw_lite.cli import main
from amw_lite.config import atomic_write_text, dump_yaml, load_yaml_file
from amw_lite.worktree import task_worktree_path
from tests.conftest import MultiRepoEnv, git_cmd


def _run_cli(ws: Path, *args: str) -> tuple[int, dict]:
    from io import StringIO
    from contextlib import redirect_stdout, redirect_stderr

    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["--workspace", str(ws), "--json", *args])
    raw = out.getvalue().strip()
    payload = json.loads(raw) if raw else {"ok": False, "stderr": err.getvalue()}
    return code, payload


def test_full_dual_repo_requirement_lifecycle_via_cli(multi_repo_env: MultiRepoEnv, tmp_path: Path) -> None:
    ws = multi_repo_env.workspace

    # Install fake `ocr` and `deploy` hooks in local.yaml
    fake_ocr = tmp_path / "ocr"
    atomic_write_text(
        fake_ocr,
        f"#!{sys.executable}\nimport json\nprint(json.dumps({{'findings': []}}))\n",
    )
    fake_ocr.chmod(fake_ocr.stat().st_mode | stat.S_IEXEC)

    fake_deploy = tmp_path / "fast-deploy"
    atomic_write_text(
        fake_deploy,
        f"#!{sys.executable}\nimport sys\nprint('DEPLOYED', sys.argv[1:])\n",
    )
    fake_deploy.chmod(fake_deploy.stat().st_mode | stat.S_IEXEC)

    local = load_yaml_file(ws / ".workspace/local.yaml")
    local["hooks_override"] = {
        "review": [str(fake_ocr), "review", "--from", "{baseline_commit}", "--to", "{head_commit}", "--format", "json"],
        "release": [str(fake_deploy), "--env", "{env}", "--plan", "{release_plan_path}"],
    }
    atomic_write_text(ws / ".workspace/local.yaml", dump_yaml(local))

    # 1. Create Requirement & approve Gate #1 (brief) and Gate #2 (design + contract)
    code, _ = _run_cli(ws, "req", "new", "REQ-100", "--title", "Multi-currency order")
    assert code == 0

    code, res = _run_cli(ws, "resume")
    assert code == 0 and res["data"]["next"]["action"] == "req.approve.brief"

    atomic_write_text(ws / "requirements/REQ-100/brief.md", "# Brief\nSupport USD and EUR checkout.\n")
    assert _run_cli(ws, "req", "approve", "REQ-100", "--gate", "brief")[0] == 0

    atomic_write_text(ws / "requirements/REQ-100/design.md", "# Design\nCore converts currency; Biz displays total.\n")
    atomic_write_text(
        ws / "contracts/currency-api.md",
        "---\nid: currency-api\nstatus: accepted\nowner: alice\n---\n\n# Currency API\nConvert(amount, currency)\n",
    )
    assert _run_cli(ws, "req", "approve", "REQ-100", "--gate", "design")[0] == 0

    # 2. Create two Tasks: T01 (core) and T02 (biz, parallel via accepted contract)
    assert _run_cli(ws, "task", "new", "REQ-100", "T01", "--title", "Core currency", "--repos", "order-core", "--contract", "contracts/currency-api.md")[0] == 0
    assert _run_cli(ws, "task", "new", "REQ-100", "T02", "--title", "Biz currency", "--repos", "order-biz", "--depends-on", "T01:contract:contracts/currency-api.md")[0] == 0
    atomic_write_text(ws / "requirements/REQ-100/tasks/T01/plan.md", "# Plan\n1. Add core currency service.\n")
    atomic_write_text(ws / "requirements/REQ-100/tasks/T02/plan.md", "# Plan\n1. Integrate biz with currency-api.\n")

    # Both can start in parallel because contract is accepted!
    assert _run_cli(ws, "task", "start", "REQ-100", "T01")[0] == 0
    assert _run_cli(ws, "task", "start", "REQ-100", "T02")[0] == 0

    wt1 = task_worktree_path(ws, "REQ-100", "T01", "order-core")
    wt2 = task_worktree_path(ws, "REQ-100", "T02", "order-biz")

    # 3. Implement in isolated worktrees, check, OCR review, fix in same branch, approve Gate #3
    atomic_write_text(wt1 / "currency.py", "RATE = 1.1\n")
    git_cmd(wt1, "add", "currency.py")
    git_cmd(wt1, "commit", "-m", "feat: add currency rate")

    atomic_write_text(wt2 / "checkout.py", "CURRENCY = 'USD'\n")
    git_cmd(wt2, "add", "checkout.py")
    git_cmd(wt2, "commit", "-m", "feat: wire biz checkout")

    assert _run_cli(ws, "task", "check", "REQ-100", "T01")[0] == 0
    assert _run_cli(ws, "task", "review", "REQ-100", "T01")[0] == 0
    # Simulate CR fix directly on T01 current branch
    atomic_write_text(wt1 / "currency.py", "RATE = 1.12  # fixed precision\n")
    git_cmd(wt1, "add", "currency.py")
    git_cmd(wt1, "commit", "-m", "fix: currency precision after ocr review")
    assert _run_cli(ws, "task", "check", "REQ-100", "T01")[0] == 0
    assert _run_cli(ws, "task", "review", "REQ-100", "T01")[0] == 0
    assert _run_cli(ws, "task", "review", "REQ-100", "T01", "--approve")[0] == 0

    assert _run_cli(ws, "task", "check", "REQ-100", "T02")[0] == 0
    assert _run_cli(ws, "task", "review", "REQ-100", "T02")[0] == 0
    assert _run_cli(ws, "task", "review", "REQ-100", "T02", "--approve")[0] == 0

    # 4. E2E Gates #4 & #5 while still in Task branches
    atomic_write_text(ws / "requirements/REQ-100/e2e-plan.md", "# E2E\n- [x] USD checkout passes\n")
    assert _run_cli(ws, "req", "approve", "REQ-100", "--gate", "e2e-plan")[0] == 0
    assert _run_cli(ws, "e2e", "run", "REQ-100")[0] == 0
    assert _run_cli(ws, "req", "approve", "REQ-100", "--gate", "e2e-result")[0] == 0

    # 5. MR handoff, human merges both branches into origin/main, then finish both Tasks
    code, mr1 = _run_cli(ws, "task", "mr", "REQ-100", "T01")
    assert code == 0 and mr1["data"]["merge_requests"][0]["source_branch"] == "amw/REQ-100/T01"
    assert _run_cli(ws, "task", "mr", "REQ-100", "T02")[0] == 0

    # Simulate human pushing & merging into main on origin
    git_cmd(multi_repo_env.core_repo, "merge", "--ff-only", "amw/REQ-100/T01")
    git_cmd(multi_repo_env.core_repo, "push", "origin", "main")
    git_cmd(multi_repo_env.biz_repo, "merge", "--ff-only", "amw/REQ-100/T02")
    git_cmd(multi_repo_env.biz_repo, "push", "origin", "main")

    assert _run_cli(ws, "task", "finish", "REQ-100", "T01")[0] == 0
    assert _run_cli(ws, "task", "finish", "REQ-100", "T02")[0] == 0

    # 6. Release plan (Gate #6), QA release hook preview + apply, PROD blocked, knowledge + close + cleanup
    assert _run_cli(ws, "release", "plan", "REQ-100")[0] == 0
    plan_text = (ws / "requirements/REQ-100/release-plan.md").read_text(encoding="utf-8")
    assert "Task `T01`" in plan_text and "Task `T02`" in plan_text

    assert _run_cli(ws, "req", "approve", "REQ-100", "--gate", "release-plan")[0] == 0
    assert _run_cli(ws, "release", "run", "REQ-100", "--env", "qa")[0] == 0
    code, rel_qa = _run_cli(ws, "release", "run", "REQ-100", "--env", "qa", "--apply")
    assert code == 0 and rel_qa["data"]["applied"] is True
    assert _run_cli(ws, "release", "run", "REQ-100", "--env", "prod", "--apply")[0] == 3

    # Knowledge scheme A: append approved learning directly into context/
    atomic_write_text(ws / "context/domain.md", "# Domain\n- Always round currency rates to 2 decimal places.\n")

    code, closed = _run_cli(ws, "req", "close", "REQ-100")
    assert code == 0 and closed["data"]["status"] == "closed"
    assert len(closed["data"]["cleanup_suggestions"]) == 2

    # Safe cleanup removes clean worktrees
    assert _run_cli(ws, "task", "cleanup", "REQ-100", "T01", "--apply")[0] == 0
    assert _run_cli(ws, "task", "cleanup", "REQ-100", "T02", "--apply")[0] == 0
    assert not wt1.exists() and not wt2.exists()
