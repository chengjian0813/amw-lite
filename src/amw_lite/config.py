"""Workspace root discovery and atomic YAML persistence."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from amw_lite.errors import EXIT_VALIDATION_ERROR, AmwLiteError
from amw_lite.models import (
    CheckEvidence,
    HookConfig,
    LocalConfig,
    RepoExecution,
    RepositoryConfig,
    Requirement,
    RequirementGates,
    ReviewEvidence,
    Task,
    TaskDependency,
    VerifyCommand,
    WorkspaceConfig,
)
from amw_lite.security import require_safe_id, resolve_within

WORKSPACE_MARKER = Path(".workspace") / "workspace.yaml"


def atomic_write_text(path: Path, content: str) -> None:
    """Write text to a sibling temp file then atomically replace target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def dump_yaml(data: dict[str, Any]) -> str:
    """Render deterministic UTF-8 YAML without mangling key order."""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Read and parse a YAML mapping file."""
    if not path.is_file():
        raise AmwLiteError(f"missing file: {path}", code="FILE_MISSING", exit_code=EXIT_VALIDATION_ERROR)
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AmwLiteError(f"invalid YAML in {path}: {exc}", code="YAML_INVALID", exit_code=EXIT_VALIDATION_ERROR) from exc
    if not isinstance(value, dict):
        raise AmwLiteError(f"YAML root must be a mapping: {path}", code="YAML_INVALID", exit_code=EXIT_VALIDATION_ERROR)
    return value


def find_workspace_root(start: Path | None = None) -> Path:
    """Walk upward from `start` (default cwd) to locate `.workspace/workspace.yaml`."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / WORKSPACE_MARKER).is_file():
            return candidate
    raise AmwLiteError(
        f"not inside an amw-lite workspace (missing {WORKSPACE_MARKER})",
        code="WORKSPACE_NOT_FOUND",
        exit_code=EXIT_VALIDATION_ERROR,
    )


def load_workspace(root: Path) -> WorkspaceConfig:
    raw = load_yaml_file(root / ".workspace" / "workspace.yaml")
    hooks_raw = raw.get("hooks") or {}
    if not isinstance(hooks_raw, dict):
        raise AmwLiteError("workspace.yaml hooks must be a mapping", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
    hooks: dict[str, HookConfig] = {}
    for name, item in hooks_raw.items():
        if not isinstance(item, dict):
            raise AmwLiteError(f"hook {name} must be a mapping", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        argv = item.get("argv") or []
        if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
            raise AmwLiteError(f"hook {name}.argv must be a list of strings", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        policy = item.get("policy") or {}
        if not isinstance(policy, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in policy.items()):
            raise AmwLiteError(f"hook {name}.policy must be a string mapping", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        hooks[name] = HookConfig(
            enabled=bool(item.get("enabled", False)),
            argv=tuple(argv),
            timeout_seconds=int(item.get("timeout_seconds", 300)),
            policy=dict(policy),
        )
    platforms = raw.get("platforms") or ["codex", "claude", "cursor"]
    if not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms):
        raise AmwLiteError("workspace.yaml platforms must be a list of strings", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
    return WorkspaceConfig(
        schema_version=int(raw.get("schema_version", 1)),
        workspace_id=str(raw.get("workspace_id", "")),
        name=str(raw.get("name", "")),
        baseline=str(raw.get("baseline", "main")),
        platforms=tuple(platforms),
        hooks=hooks,
    )


def load_repositories(root: Path) -> tuple[RepositoryConfig, ...]:
    raw = load_yaml_file(root / ".workspace" / "repositories.yaml")
    items = raw.get("repositories")
    if not isinstance(items, list) or not items:
        raise AmwLiteError("repositories.yaml must declare a non-empty repositories list", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
    repos: list[RepositoryConfig] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise AmwLiteError("each repository entry must be a mapping", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        repo_id = str(item.get("repo_id", ""))
        if repo_id in seen:
            raise AmwLiteError(f"duplicate repo_id: {repo_id}", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        seen.add(repo_id)
        verify = item.get("default_verify") or []
        if not isinstance(verify, list) or not all(isinstance(v, str) for v in verify):
            raise AmwLiteError(f"default_verify for {repo_id} must be a list of strings", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        repos.append(
            RepositoryConfig(
                repo_id=repo_id,
                name=str(item.get("name", repo_id)),
                baseline=str(item.get("baseline", "main")),
                role=str(item.get("role", "service")),
                remote=str(item.get("remote", "")),
                default_verify=tuple(verify),
            )
        )
    return tuple(repos)


def load_local_config(root: Path) -> LocalConfig:
    raw = load_yaml_file(root / ".workspace" / "local.yaml")
    repos_raw = raw.get("repositories")
    if not isinstance(repos_raw, dict):
        raise AmwLiteError("local.yaml repositories must be a mapping", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
    known = {r.repo_id for r in load_repositories(root)}
    resolved: dict[str, str] = {}
    for repo_id, raw_path in repos_raw.items():
        if repo_id not in known:
            raise AmwLiteError(f"local.yaml references unknown repo_id: {repo_id}", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise AmwLiteError(f"local.yaml path for {repo_id} must be a non-empty string", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        expanded = Path(raw_path).expanduser()
        if not expanded.is_absolute():
            raise AmwLiteError(f"local.yaml path for {repo_id} must be absolute: {raw_path}", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        resolved[repo_id] = str(expanded.resolve())
    wt_root_raw = raw.get("worktree_root")
    wt_root: str | None = None
    if wt_root_raw is not None:
        if not isinstance(wt_root_raw, str) or not Path(wt_root_raw).expanduser().is_absolute():
            raise AmwLiteError("local.yaml worktree_root must be an absolute path or null", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
        wt_root = str(Path(wt_root_raw).expanduser().resolve())
    hooks_override = raw.get("hooks_override") or {}
    if not isinstance(hooks_override, dict):
        raise AmwLiteError("local.yaml hooks_override must be a mapping", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
    return LocalConfig(
        schema_version=int(raw.get("schema_version", 1)),
        repositories=resolved,
        worktree_root=wt_root,
        hooks_override={str(k): list(v) for k, v in hooks_override.items() if isinstance(v, list)},
    )


def worktree_base_dir(root: Path) -> Path:
    local = load_local_config(root)
    return Path(local.worktree_root) if local.worktree_root else (root.resolve() / ".worktrees")


def requirement_dir(root: Path, req_id: str) -> Path:
    require_safe_id("requirement_id", req_id)
    return root / "requirements" / req_id


def task_dir(root: Path, req_id: str, task_id: str) -> Path:
    require_safe_id("requirement_id", req_id)
    require_safe_id("task_id", task_id)
    return requirement_dir(root, req_id) / "tasks" / task_id


def load_requirement(root: Path, req_id: str) -> Requirement:
    raw = load_yaml_file(requirement_dir(root, req_id) / "requirement.yaml")
    gates_raw = raw.get("gates") or {}
    if not isinstance(gates_raw, dict):
        raise AmwLiteError("requirement.yaml gates must be a mapping", code="SCHEMA_INVALID", exit_code=EXIT_VALIDATION_ERROR)
    gates = RequirementGates(
        brief_approved=bool(gates_raw.get("brief_approved", False)),
        design_approved=bool(gates_raw.get("design_approved", False)),
        e2e_plan_approved=bool(gates_raw.get("e2e_plan_approved", False)),
        e2e_result_approved=bool(gates_raw.get("e2e_result_approved", False)),
        release_plan_approved=bool(gates_raw.get("release_plan_approved", False)),
    )
    return Requirement(
        schema_version=int(raw.get("schema_version", 1)),
        requirement_id=str(raw.get("requirement_id", req_id)),
        title=str(raw.get("title", "")),
        kind=str(raw.get("kind", "feature")),
        owner=str(raw.get("owner", "")),
        status=str(raw.get("status", "open")),
        external_ref=str(raw.get("external_ref", "")),
        gates=gates,
    )


def save_requirement(root: Path, req: Requirement) -> None:
    atomic_write_text(requirement_dir(root, req.requirement_id) / "requirement.yaml", dump_yaml(req.to_dict()))


def list_requirements(root: Path) -> tuple[Requirement, ...]:
    req_root = root / "requirements"
    if not req_root.is_dir():
        return ()
    items = []
    for child in sorted(req_root.iterdir()):
        if child.is_dir() and (child / "requirement.yaml").is_file():
            items.append(load_requirement(root, child.name))
    return tuple(items)


def load_task(root: Path, req_id: str, task_id: str) -> Task:
    raw = load_yaml_file(task_dir(root, req_id, task_id) / "task.yaml")
    deps = tuple(
        TaskDependency(
            task_id=str(d["task_id"]),
            dep_type=str(d.get("type", "hard_code")),
            contract=d.get("contract"),
        )
        for d in (raw.get("depends_on") or [])
    )
    cmds = tuple(
        VerifyCommand(repo_id=str(c["repo_id"]), command=str(c["command"]))
        for c in (raw.get("verify_commands") or [])
    )
    exec_raw = raw.get("execution") or {}
    executions = {
        repo_id: RepoExecution(
            branch=str(val["branch"]),
            baseline=str(val["baseline"]),
            baseline_commit=str(val["baseline_commit"]),
        )
        for repo_id, val in (exec_raw.get("repositories") or {}).items()
    }
    ev_raw = raw.get("evidence") or {}
    checks = tuple(
        CheckEvidence(
            repo_id=str(c["repo_id"]),
            command=str(c["command"]),
            commit=str(c["commit"]),
            exit_code=int(c["exit_code"]),
            clean_before=bool(c["clean_before"]),
            clean_after=bool(c["clean_after"]),
            executed_at=str(c["executed_at"]),
        )
        for c in (ev_raw.get("checks") or [])
    )
    reviews = tuple(
        ReviewEvidence(
            repo_id=str(r["repo_id"]),
            tool=str(r["tool"]),
            commit=str(r["commit"]),
            findings_count=int(r.get("findings_count", 0)),
            human_approved=bool(r.get("human_approved", False)),
            reviewed_at=str(r["reviewed_at"]),
            report_path=str(r.get("report_path", "")),
        )
        for r in (ev_raw.get("reviews") or [])
    )
    return Task(
        schema_version=int(raw.get("schema_version", 1)),
        requirement_id=str(raw.get("requirement_id", req_id)),
        task_id=str(raw.get("task_id", task_id)),
        title=str(raw.get("title", "")),
        assignee=str(raw.get("assignee", "")),
        status=str(raw.get("status", "draft")),
        repositories=tuple(str(r) for r in (raw.get("repositories") or [])),
        contracts=tuple(str(c) for c in (raw.get("contracts") or [])),
        depends_on=deps,
        context_files=tuple(str(c) for c in (raw.get("context_files") or [])),
        verify_commands=cmds,
        started_at=exec_raw.get("started_at"),
        executions=executions,
        checks=checks,
        reviews=reviews,
    )


def save_task(root: Path, task: Task) -> None:
    atomic_write_text(task_dir(root, task.requirement_id, task.task_id) / "task.yaml", dump_yaml(task.to_dict()))


def list_tasks(root: Path, req_id: str) -> tuple[Task, ...]:
    t_root = requirement_dir(root, req_id) / "tasks"
    if not t_root.is_dir():
        return ()
    items = []
    for child in sorted(t_root.iterdir()):
        if child.is_dir() and (child / "task.yaml").is_file():
            items.append(load_task(root, req_id, child.name))
    return tuple(items)


def read_contract_status(root: Path, relative_path: str) -> str | None:
    """Parse `status` from YAML frontmatter of a contract markdown file."""
    path = resolve_within(root, relative_path, label="contract")
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        frontmatter = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    if not isinstance(frontmatter, dict):
        return None
    status = frontmatter.get("status")
    return str(status) if isinstance(status, str) else None
