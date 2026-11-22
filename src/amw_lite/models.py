"""Core immutable data models for amw-lite (pure Python, zero IO)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from amw_lite.errors import EXIT_VALIDATION_ERROR, AmwLiteError
from amw_lite.security import require_safe_id, require_sha

SUPPORTED_PLATFORMS = ("codex", "claude", "cursor")
REQUIREMENT_KINDS = ("feature", "bugfix")
REQUIREMENT_STATUSES = ("open", "closed", "cancelled")
TASK_STATUSES = ("draft", "active", "done", "cancelled")
DEPENDENCY_TYPES = ("hard_code", "contract")
GATE_NAMES = ("brief", "design", "e2e-plan", "e2e-result", "release-plan")
RELEASE_ENV_POLICIES = {
    "qa": ("off", "ask"),
    "pre": ("off", "ask"),
    "prod": ("manual",),
}


@dataclass(frozen=True)
class HookConfig:
    """External CLI hook template executed strictly via argv without shell."""

    enabled: bool = False
    argv: tuple[str, ...] = ()
    timeout_seconds: int = 300
    policy: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "enabled": self.enabled,
            "argv": list(self.argv),
        }
        if self.timeout_seconds != 300:
            data["timeout_seconds"] = self.timeout_seconds
        if self.policy:
            data["policy"] = dict(self.policy)
        return data


@dataclass(frozen=True)
class WorkspaceConfig:
    """Committed `.workspace/workspace.yaml` identity and hook definitions."""

    workspace_id: str
    name: str
    baseline: str = "main"
    platforms: tuple[str, ...] = ("codex", "claude", "cursor")
    hooks: dict[str, HookConfig] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise AmwLiteError("workspace.yaml schema_version must be 1", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        require_safe_id("workspace_id", self.workspace_id)
        if not self.name.strip():
            raise AmwLiteError("workspace name must not be empty", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        if not self.baseline.strip():
            raise AmwLiteError("workspace baseline must not be empty", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        for platform in self.platforms:
            if platform not in SUPPORTED_PLATFORMS:
                raise AmwLiteError(f"unsupported platform: {platform}", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        release_hook = self.hooks.get("release")
        if release_hook and release_hook.policy:
            for env, allowed in RELEASE_ENV_POLICIES.items():
                val = release_hook.policy.get(env)
                if val is not None and val not in allowed:
                    raise AmwLiteError(
                        f"invalid release policy for {env}: {val!r} (allowed: {', '.join(allowed)})",
                        code="SCHEMA_INVALID",
                        exit_code=EXIT_VALIDATION_ERROR,
                    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "baseline": self.baseline,
            "platforms": list(self.platforms),
            "hooks": {k: v.to_dict() for k, v in self.hooks.items()},
        }


@dataclass(frozen=True)
class RepositoryConfig:
    """One logical business repository registered in `.workspace/repositories.yaml`."""

    repo_id: str
    name: str
    baseline: str = "main"
    role: str = "service"
    remote: str = ""
    default_verify: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_safe_id("repo_id", self.repo_id)
        if not self.name.strip():
            raise AmwLiteError(f"repository {self.repo_id} name must not be empty", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        if not self.baseline.strip():
            raise AmwLiteError(f"repository {self.repo_id} baseline must not be empty", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "repo_id": self.repo_id,
            "name": self.name,
            "baseline": self.baseline,
            "role": self.role,
        }
        if self.remote:
            data["remote"] = self.remote
        if self.default_verify:
            data["default_verify"] = list(self.default_verify)
        return data


@dataclass(frozen=True)
class LocalConfig:
    """Gitignored `.workspace/local.yaml` machine-local paths and optional hook overrides."""

    repositories: dict[str, str]
    worktree_root: str | None = None
    hooks_override: dict[str, list[str]] = field(default_factory=dict)
    schema_version: int = 1


@dataclass(frozen=True)
class RequirementGates:
    """Human sign-off gates aligned with the core decision points."""

    brief_approved: bool = False
    design_approved: bool = False
    e2e_plan_approved: bool = False
    e2e_result_approved: bool = False
    release_plan_approved: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "brief_approved": self.brief_approved,
            "design_approved": self.design_approved,
            "e2e_plan_approved": self.e2e_plan_approved,
            "e2e_result_approved": self.e2e_result_approved,
            "release_plan_approved": self.release_plan_approved,
        }


@dataclass(frozen=True)
class Requirement:
    """Requirement metadata stored at `requirements/<req-id>/requirement.yaml`."""

    requirement_id: str
    title: str
    kind: str = "feature"
    owner: str = ""
    status: str = "open"
    external_ref: str = ""
    gates: RequirementGates = field(default_factory=RequirementGates)
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_safe_id("requirement_id", self.requirement_id)
        if self.kind not in REQUIREMENT_KINDS:
            raise AmwLiteError(f"invalid requirement kind: {self.kind}", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        if self.status not in REQUIREMENT_STATUSES:
            raise AmwLiteError(f"invalid requirement status: {self.status}", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        if not self.title.strip():
            raise AmwLiteError("requirement title must not be empty", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "requirement_id": self.requirement_id,
            "title": self.title,
            "kind": self.kind,
            "owner": self.owner,
            "status": self.status,
            "external_ref": self.external_ref,
            "gates": self.gates.to_dict(),
        }


@dataclass(frozen=True)
class TaskDependency:
    """Directed edge to an upstream Task inside the same Requirement."""

    task_id: str
    dep_type: str = "hard_code"
    contract: str | None = None

    def __post_init__(self) -> None:
        require_safe_id("depends_on.task_id", self.task_id)
        if self.dep_type not in DEPENDENCY_TYPES:
            raise AmwLiteError(f"invalid dependency type: {self.dep_type}", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        if self.dep_type == "contract" and not self.contract:
            raise AmwLiteError("contract dependency requires a contract path", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"task_id": self.task_id, "type": self.dep_type}
        if self.contract:
            data["contract"] = self.contract
        return data


@dataclass(frozen=True)
class VerifyCommand:
    """One repo-bound verification command."""

    repo_id: str
    command: str

    def __post_init__(self) -> None:
        require_safe_id("repo_id", self.repo_id)
        if not self.command.strip():
            raise AmwLiteError("verify command must not be empty", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)

    def to_dict(self) -> dict[str, str]:
        return {"repo_id": self.repo_id, "command": self.command}


@dataclass(frozen=True)
class RepoExecution:
    """Frozen baseline and branch recorded when a Task starts."""

    branch: str
    baseline: str
    baseline_commit: str

    def __post_init__(self) -> None:
        if not self.branch.strip() or not self.baseline.strip():
            raise AmwLiteError("execution branch and baseline must not be empty", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        require_sha("baseline_commit", self.baseline_commit)

    def to_dict(self) -> dict[str, str]:
        return {
            "branch": self.branch,
            "baseline": self.baseline,
            "baseline_commit": self.baseline_commit,
        }


@dataclass(frozen=True)
class CheckEvidence:
    """Commit-bound verification command proof with pre/post clean attestation."""

    repo_id: str
    command: str
    commit: str
    exit_code: int
    clean_before: bool
    clean_after: bool
    executed_at: str

    @property
    def is_trusted_pass(self) -> bool:
        return self.exit_code == 0 and self.clean_before and self.clean_after

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "command": self.command,
            "commit": self.commit,
            "exit_code": self.exit_code,
            "clean_before": self.clean_before,
            "clean_after": self.clean_after,
            "executed_at": self.executed_at,
        }


@dataclass(frozen=True)
class ReviewEvidence:
    """Commit-bound external Code Review (e.g. ocr) summary + human sign-off."""

    repo_id: str
    tool: str
    commit: str
    findings_count: int
    human_approved: bool
    reviewed_at: str
    report_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "repo_id": self.repo_id,
            "tool": self.tool,
            "commit": self.commit,
            "findings_count": self.findings_count,
            "human_approved": self.human_approved,
            "reviewed_at": self.reviewed_at,
        }
        if self.report_path:
            data["report_path"] = self.report_path
        return data


@dataclass(frozen=True)
class Task:
    """Single source of truth for one Task at `requirements/<req>/tasks/<tid>/task.yaml`."""

    requirement_id: str
    task_id: str
    title: str
    repositories: tuple[str, ...]
    assignee: str = ""
    status: str = "draft"
    contracts: tuple[str, ...] = ()
    depends_on: tuple[TaskDependency, ...] = ()
    context_files: tuple[str, ...] = ()
    verify_commands: tuple[VerifyCommand, ...] = ()
    started_at: str | None = None
    executions: dict[str, RepoExecution] = field(default_factory=dict)
    checks: tuple[CheckEvidence, ...] = ()
    reviews: tuple[ReviewEvidence, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_safe_id("requirement_id", self.requirement_id)
        require_safe_id("task_id", self.task_id)
        if not self.title.strip():
            raise AmwLiteError("task title must not be empty", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        if self.status not in TASK_STATUSES:
            raise AmwLiteError(f"invalid task status: {self.status}", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        if not self.repositories:
            raise AmwLiteError("task must declare at least one repository", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        if len(set(self.repositories)) != len(self.repositories):
            raise AmwLiteError("task repositories must be unique", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        for repo_id in self.repositories:
            require_safe_id("repo_id", repo_id)
        for dep in self.depends_on:
            if dep.task_id == self.task_id:
                raise AmwLiteError(f"task {self.task_id} cannot depend on itself", code="DEPENDENCY_CYCLE", exit_code=EXIT_VALIDATION_ERROR)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "requirement_id": self.requirement_id,
            "task_id": self.task_id,
            "title": self.title,
            "assignee": self.assignee,
            "status": self.status,
            "repositories": list(self.repositories),
            "contracts": list(self.contracts),
            "depends_on": [d.to_dict() for d in self.depends_on],
            "context_files": list(self.context_files),
            "verify_commands": [c.to_dict() for c in self.verify_commands],
        }
        if self.started_at or self.executions:
            data["execution"] = {
                "started_at": self.started_at,
                "repositories": {k: v.to_dict() for k, v in self.executions.items()},
            }
        if self.checks or self.reviews:
            data["evidence"] = {
                "checks": [c.to_dict() for c in self.checks],
                "reviews": [r.to_dict() for r in self.reviews],
            }
        return data
