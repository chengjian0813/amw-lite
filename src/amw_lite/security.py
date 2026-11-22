"""Security guards: identifier validation, path containment, and secret/placeholder scanning."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from amw_lite.errors import EXIT_SAFETY_BLOCK, EXIT_VALIDATION_ERROR, AmwLiteError

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HEX_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SENSITIVE_ASSIGN_RE = re.compile(
    r"(?i)[\"']?(?:authorization|cookie|password|passwd|secret|token|"
    r"access[-_ ]?key|api[-_ ]?key|client[-_ ]?secret|private[-_ ]?key)"
    r"[\"']?\s*[:=]\s*(?P<val>[^\s,;}\"']+)"
)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_CREDENTIAL_URI_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@")
_LOCAL_ABS_PATH_RE = re.compile(r"(?:^|[\s\"'(=])(?:/Users/|/home/|/private/|[A-Za-z]:\\)")
_UNRESOLVED_PLACEHOLDER_RE = re.compile(r"(?:\{\{[^{}\r\n]{1,64}\}\}|<[a-z][a-z0-9_-]{1,48}>)")
_TBD_LINE_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:\[[ xX]\]\s*)?TBD\.?\s*$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_REDACTED_MARKERS = frozenset({"***", "[redacted]", "<redacted>", "changeme", "placeholder", "null", "none", '""', "''"})


@dataclass(frozen=True)
class SecurityFinding:
    """One redacted static security finding without echoing secret payloads."""

    code: str
    path: str
    line: int
    message: str


def require_safe_id(label: str, value: str) -> str:
    """Validate an identifier used in filenames, branch names, or YAML keys."""
    if not isinstance(value, str) or value in (".", "..") or not _SAFE_ID_RE.fullmatch(value):
        raise AmwLiteError(
            f"invalid {label}: {value!r} (expected alphanumeric/._- starting with alphanumeric)",
            code="INVALID_IDENTIFIER",
            exit_code=EXIT_VALIDATION_ERROR,
        )
    return value


def require_sha(label: str, value: str) -> str:
    """Validate a full 40-char (or SHA-256 64-char) Git commit hash."""
    if not isinstance(value, str) or not _HEX_SHA_RE.fullmatch(value):
        raise AmwLiteError(
            f"invalid {label} commit SHA: {value!r}",
            code="INVALID_COMMIT_SHA",
            exit_code=EXIT_VALIDATION_ERROR,
        )
    return value


def resolve_within(root: Path, relative: str, *, label: str = "path") -> Path:
    """Resolve a workspace-relative path and reject traversal or escaping symlinks."""
    if not isinstance(relative, str) or not relative.strip():
        raise AmwLiteError(f"{label} must be a non-empty relative path", code="UNSAFE_PATH", exit_code=EXIT_VALIDATION_ERROR)
    if relative.startswith(("/", "\\", "~")) or ":" in relative.split("/")[0]:
        raise AmwLiteError(f"{label} must be relative, not absolute: {relative}", code="UNSAFE_PATH", exit_code=EXIT_SAFETY_BLOCK)
    parts = Path(relative).parts
    if any(part in ("..", "") for part in parts):
        raise AmwLiteError(f"{label} escapes root: {relative}", code="UNSAFE_PATH", exit_code=EXIT_SAFETY_BLOCK)
    root_resolved = root.resolve()
    target = (root_resolved / relative).resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise AmwLiteError(
            f"{label} resolves outside workspace root: {relative}",
            code="UNSAFE_PATH",
            exit_code=EXIT_SAFETY_BLOCK,
        ) from exc
    return target


def scan_text_for_secrets(text: str, *, path_label: str, check_local_paths: bool = True) -> tuple[SecurityFinding, ...]:
    """Scan committed config/spec/evidence text for leaked credentials or local paths."""
    findings: list[SecurityFinding] = []
    for idx, line in enumerate(text.splitlines(), 1):
        if _PRIVATE_KEY_RE.search(line):
            findings.append(SecurityFinding("SECRET_PRIVATE_KEY", path_label, idx, "private key header detected"))
        if _CREDENTIAL_URI_RE.search(line):
            findings.append(SecurityFinding("SECRET_CREDENTIAL_URI", path_label, idx, "URI with embedded credentials detected"))
        for match in _SENSITIVE_ASSIGN_RE.finditer(line):
            raw_val = match.group("val").strip("\"'").lower()
            if raw_val and raw_val not in _REDACTED_MARKERS and not raw_val.startswith(("$", "{")):
                findings.append(SecurityFinding("SECRET_LITERAL", path_label, idx, "possible hardcoded secret or token detected"))
                break
        if check_local_paths and _LOCAL_ABS_PATH_RE.search(line):
            findings.append(SecurityFinding("LOCAL_ABSOLUTE_PATH", path_label, idx, "machine-local absolute path detected"))
    return tuple(findings)


def find_unresolved_placeholders(text: str) -> tuple[str, ...]:
    """Return any unresolved template placeholders (e.g. {{foo}} or <repo-path>)."""
    return tuple(sorted(set(_UNRESOLVED_PLACEHOLDER_RE.findall(text))))


def check_markdown_complete(path: Path) -> str | None:
    """Return an error reason if a Spec markdown file is missing, empty, heading-only, or contains TBD."""
    if not path.is_file():
        return f"missing required spec file: {path.name}"
    text = path.read_text(encoding="utf-8")
    visible = _COMMENT_RE.sub("", text)
    non_empty = [line.strip() for line in visible.splitlines() if line.strip()]
    if not non_empty or all(_HEADING_RE.match(line) for line in non_empty):
        return f"{path.name} has no substantive content (empty or headings only)"
    for line in text.splitlines():
        if _TBD_LINE_RE.match(line):
            return f"{path.name} still contains unresolved TBD placeholder"
    return None
