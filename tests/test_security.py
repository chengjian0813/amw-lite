"""Security unit tests: identifiers, path containment, secret scanning, hook placeholders."""

from __future__ import annotations

from pathlib import Path

import pytest

from amw_lite.errors import AmwLiteError
from amw_lite.hooks import resolve_hook_argv
from amw_lite.security import (
    check_markdown_complete,
    find_unresolved_placeholders,
    require_safe_id,
    resolve_within,
    scan_text_for_secrets,
)


def test_safe_identifier_and_path_containment(tmp_path: Path) -> None:
    assert require_safe_id("task_id", "T01-core_v1") == "T01-core_v1"
    with pytest.raises(AmwLiteError):
        require_safe_id("task_id", "../escape")
    with pytest.raises(AmwLiteError):
        resolve_within(tmp_path, "../outside.md")
    with pytest.raises(AmwLiteError):
        resolve_within(tmp_path, "/etc/passwd")
    assert resolve_within(tmp_path, "contracts/api.md") == (tmp_path / "contracts/api.md").resolve()


def test_secret_and_local_path_scanning() -> None:
    sample = (
        "api_key: supersecret12345\n"
        "url: https://user:pass@git.example.invalid/repo.git\n"
        "path: /Users/alice/projects/order-core\n"
        "safe_redacted: ***\n"
    )
    findings = scan_text_for_secrets(sample, path_label="design.md")
    codes = {f.code for f in findings}
    assert {"SECRET_LITERAL", "SECRET_CREDENTIAL_URI", "LOCAL_ABSOLUTE_PATH"} <= codes
    assert all(f.line != 4 for f in findings)


def test_unresolved_placeholders_and_hook_argv() -> None:
    assert find_unresolved_placeholders("run <task-worktree> {{env}}") == ("<task-worktree>", "{{env}}")
    argv = resolve_hook_argv(("ocr", "review", "--from", "{base}", "--to", "{head}"), {"base": "aaa", "head": "bbb"})
    assert argv == ("ocr", "review", "--from", "aaa", "--to", "bbb")
    with pytest.raises(AmwLiteError, match="unknown hook template variable"):
        resolve_hook_argv(("ocr", "{missing}"), {})
    with pytest.raises(AmwLiteError, match="unresolved placeholder"):
        resolve_hook_argv(("ocr", "--bg", "{bg}"), {"bg": "see <unfilled-doc>"})


def test_markdown_completeness_check(tmp_path: Path) -> None:
    p = tmp_path / "brief.md"
    p.write_text("# Title Only\n## Section\n", encoding="utf-8")
    assert check_markdown_complete(p) is not None

    p.write_text("# Brief\n- TBD\n", encoding="utf-8")
    assert check_markdown_complete(p) is not None

    p.write_text("# Brief\nSupport multi-currency checkout across core and biz.\n", encoding="utf-8")
    assert check_markdown_complete(p) is None
