"""Single-source Companion Skill sync and drift detection across Agent platforms."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from amw_lite.config import atomic_write_text, dump_yaml, load_workspace, load_yaml_file
from amw_lite.errors import EXIT_SAFETY_BLOCK, EXIT_VALIDATION_ERROR, AmwLiteError

PLATFORM_SKILL_ROOTS: dict[str, Path] = {
    "codex": Path(".agents/skills"),
    "claude": Path(".claude/skills"),
    "cursor": Path(".cursor/skills"),
}
LOCK_PATH = Path(".workspace/skills.lock.yaml")


@dataclass(frozen=True)
class SkillSyncAction:
    skill: str
    platform: str
    target: Path


@dataclass(frozen=True)
class SkillIssue:
    code: str
    skill: str
    message: str


def digest_directory(directory: Path) -> str:
    """Deterministic SHA-256 over relative paths and bytes in a skill directory."""
    h = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        rel = path.relative_to(directory).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _source_skills(root: Path) -> dict[str, Path]:
    src = root / ".workspace" / "skills-src"
    if not src.is_dir():
        raise AmwLiteError("missing .workspace/skills-src directory", code="SKILLS_SRC_MISSING", exit_code=EXIT_VALIDATION_ERROR)
    skills = {p.name: p for p in sorted(src.iterdir()) if p.is_dir()}
    if not skills:
        raise AmwLiteError("no skill directories found in .workspace/skills-src", code="SKILLS_SRC_EMPTY", exit_code=EXIT_VALIDATION_ERROR)
    return skills


def _load_lock(root: Path) -> dict[str, str]:
    lock_file = root / LOCK_PATH
    if not lock_file.is_file():
        return {}
    raw = load_yaml_file(lock_file)
    entries = raw.get("skills") or {}
    return {str(k): str(v) for k, v in entries.items()} if isinstance(entries, dict) else {}


def sync_skills(root: Path, *, apply: bool = False) -> tuple[SkillSyncAction, ...]:
    """Preview or sync `.workspace/skills-src/*` into enabled platform skill folders without clobbering drift."""
    ws = load_workspace(root)
    sources = _source_skills(root)
    locked = _load_lock(root)
    actions: list[SkillSyncAction] = []

    for skill_name, src_dir in sources.items():
        src_digest = digest_directory(src_dir)
        old_digest = locked.get(skill_name)
        for platform in ws.platforms:
            target = root / PLATFORM_SKILL_ROOTS[platform] / skill_name
            if target.exists():
                if not target.is_dir():
                    raise AmwLiteError(f"skill target is not a directory: {target}", code="SKILL_CONFLICT", exit_code=EXIT_SAFETY_BLOCK)
                cur_digest = digest_directory(target)
                if cur_digest == src_digest:
                    continue
                if old_digest is None or cur_digest != old_digest:
                    raise AmwLiteError(
                        f"manual drift detected at {target.relative_to(root)}; reconcile into .workspace/skills-src/{skill_name} before syncing",
                        code="SKILL_DRIFT",
                        exit_code=EXIT_SAFETY_BLOCK,
                    )
            actions.append(SkillSyncAction(skill=skill_name, platform=platform, target=target))

    if not apply:
        return tuple(actions)

    for action in actions:
        if action.target.exists():
            shutil.rmtree(action.target)
        shutil.copytree(sources[action.skill], action.target)

    new_lock = {
        "schema_version": 1,
        "skills": {name: digest_directory(path) for name, path in sources.items()},
    }
    atomic_write_text(root / LOCK_PATH, dump_yaml(new_lock))
    return tuple(actions)


def check_skills(root: Path) -> tuple[SkillIssue, ...]:
    """Verify source lock and all platform copies match without modifying files."""
    ws = load_workspace(root)
    sources = _source_skills(root)
    locked = _load_lock(root)
    issues: list[SkillIssue] = []

    for skill_name, src_dir in sources.items():
        src_digest = digest_directory(src_dir)
        if locked.get(skill_name) != src_digest:
            issues.append(SkillIssue("SKILL_LOCK_STALE", skill_name, "source differs from skills.lock.yaml; run `amw-lite skills sync --apply`"))
        for platform in ws.platforms:
            target = root / PLATFORM_SKILL_ROOTS[platform] / skill_name
            if not target.is_dir():
                issues.append(SkillIssue("SKILL_MISSING", skill_name, f"missing synced skill for {platform}: {target.relative_to(root)}"))
            elif digest_directory(target) != src_digest:
                issues.append(SkillIssue("SKILL_DRIFT", skill_name, f"skill copy drifted for {platform}: {target.relative_to(root)}"))
    return tuple(issues)
