"""Tests for single-source Skill sync/drift detection and read-only `doctor` diagnostics."""

from __future__ import annotations

import pytest

from amw_lite.config import atomic_write_text
from amw_lite.doctor import run_doctor
from amw_lite.errors import AmwLiteError
from amw_lite.skills import check_skills, sync_skills
from tests.conftest import MultiRepoEnv


def test_skill_sync_and_drift_protection(multi_repo_env: MultiRepoEnv) -> None:
    ws = multi_repo_env.workspace
    assert check_skills(ws) == ()

    # Update canonical source -> check reports drift/stale lock, sync --apply updates all 3 platforms
    src = ws / ".workspace/skills-src/amw-lite/SKILL.md"
    atomic_write_text(src, src.read_text(encoding="utf-8") + "\n<!-- updated -->\n")
    issues = check_skills(ws)
    assert any(i.code == "SKILL_LOCK_STALE" for i in issues)

    actions = sync_skills(ws, apply=False)
    assert len(actions) == 3
    sync_skills(ws, apply=True)
    assert check_skills(ws) == ()

    # Hand-edit a generated platform copy -> sync must refuse to overwrite
    claude_copy = ws / ".claude/skills/amw-lite/SKILL.md"
    atomic_write_text(claude_copy, "# manually tampered\n")
    with pytest.raises(AmwLiteError, match="manual drift detected"):
        sync_skills(ws, apply=True)


def test_doctor_clean_and_secret_detection(multi_repo_env: MultiRepoEnv) -> None:
    ws = multi_repo_env.workspace
    diags = run_doctor(ws)
    assert not any(d.severity == "ERROR" for d in diags), diags

    # Plant a secret and a machine-local absolute path in context/ -> doctor catches both without echoing secret
    atomic_write_text(
        ws / "context/leak.md",
        "token: my_super_secret_token_999\ncheckout: /Users/alice/projects/order-core\n",
    )
    diags_leak = run_doctor(ws)
    errors = [d for d in diags_leak if d.severity == "ERROR"]
    codes = {d.code for d in errors}
    assert {"SECRET_LITERAL", "LOCAL_ABSOLUTE_PATH"} <= codes
    assert all("my_super_secret_token_999" not in d.message for d in errors)
