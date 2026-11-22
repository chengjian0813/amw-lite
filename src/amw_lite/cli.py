"""CLI entrypoint for `amw-lite` with uniform `--json` and concise human output."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from amw_lite import __version__
from amw_lite.check import run_task_check
from amw_lite.config import find_workspace_root, load_task
from amw_lite.doctor import run_doctor
from amw_lite.errors import EXIT_OK, EXIT_VALIDATION_ERROR, AmwLiteError
from amw_lite.hooks import run_e2e_hook, run_release_hook, run_task_review
from amw_lite.models import TaskDependency, VerifyCommand
from amw_lite.skills import check_skills, sync_skills
from amw_lite.workspace_ops import (
    approve_requirement_gate,
    build_resume_view,
    build_task_mr_plans,
    close_requirement,
    create_requirement,
    create_task,
    finish_task,
    generate_release_plan,
    init_workspace,
    start_task,
)
from amw_lite.worktree import apply_cleanup_worktrees, plan_cleanup_worktrees


def _parse_repo_spec(raw: str) -> dict[str, Any]:
    """Parse either a JSON object or compact `repo_id=path[:baseline[:remote]]` spec."""
    text = raw.strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AmwLiteError(f"invalid --repo JSON: {exc}", code="INVALID_ARGUMENT", exit_code=EXIT_VALIDATION_ERROR) from exc
        if not isinstance(obj, dict) or "repo_id" not in obj:
            raise AmwLiteError("--repo JSON must be an object containing repo_id", code="INVALID_ARGUMENT", exit_code=EXIT_VALIDATION_ERROR)
        return obj
    if "=" in text:
        repo_id, path = text.split("=", 1)
        return {"repo_id": repo_id.strip(), "path": path.strip()}
    return {"repo_id": text}


def _parse_dep_spec(raw: str) -> TaskDependency:
    """Parse `T01` (hard_code) or `T01:contract:contracts/foo.md`."""
    parts = raw.split(":", 2)
    if len(parts) == 1:
        return TaskDependency(task_id=parts[0], dep_type="hard_code")
    if len(parts) == 2 and parts[1] == "hard_code":
        return TaskDependency(task_id=parts[0], dep_type="hard_code")
    if len(parts) == 3 and parts[1] == "contract":
        return TaskDependency(task_id=parts[0], dep_type="contract", contract=parts[2])
    raise AmwLiteError(
        f"invalid --depends-on format: {raw!r} (use TASK_ID or TASK_ID:contract:contracts/path.md)",
        code="INVALID_ARGUMENT",
        exit_code=EXIT_VALIDATION_ERROR,
    )


def _parse_verify_spec(raw: str) -> VerifyCommand:
    if ":" not in raw:
        raise AmwLiteError(
            f"invalid --verify format: {raw!r} (use repo_id:command)",
            code="INVALID_ARGUMENT",
            exit_code=EXIT_VALIDATION_ERROR,
        )
    repo_id, cmd = raw.split(":", 1)
    return VerifyCommand(repo_id=repo_id.strip(), command=cmd.strip())


def _extract_global_flags(argv: Sequence[str]) -> tuple[list[str], bool, str | None]:
    """Allow `--json` and `--workspace DIR` before or after subcommands."""
    cleaned: list[str] = []
    json_mode = False
    workspace: str | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--json":
            json_mode = True
            i += 1
        elif tok == "--workspace" and i + 1 < len(argv):
            workspace = argv[i + 1]
            i += 2
        elif tok.startswith("--workspace="):
            workspace = tok.split("=", 1)[1]
            i += 1
        else:
            cleaned.append(tok)
            i += 1
    return cleaned, json_mode, workspace


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amw-lite",
        description="Lightweight open-source multi-repo AI Agent Workspace CLI.",
    )
    parser.add_argument("--version", action="version", version=f"amw-lite {__version__}")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON output")
    parser.add_argument("--workspace", default=None, help="Path inside the Workspace control repository")
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Initialize a new amw-lite Workspace control repository")
    p_init.add_argument("--target", default=".", help="Target directory for the Workspace")
    p_init.add_argument("--id", dest="workspace_id", required=True, help="Stable workspace_id")
    p_init.add_argument("--name", required=True, help="Human-readable workspace name")
    p_init.add_argument("--baseline", default="main", help="Control repo baseline branch")
    p_init.add_argument("--repo", action="append", required=True, help="Repo spec: `repo_id=/abs/path` or JSON object")
    p_init.add_argument("--platform", action="append", default=None, choices=("codex", "claude", "cursor"))
    p_init.add_argument("--dry-run", action="store_true", help="Preview files without writing")

    # doctor
    sub.add_parser("doctor", help="Run read-only environment, git, skill, hook, and security diagnostics")

    # resume
    p_res = sub.add_parser("resume", help="Inspect Workspace state and return the single recommended NEXT")
    p_res.add_argument("--req", dest="req_id", default=None, help="Specific requirement_id")
    p_res.add_argument("--task", dest="task_id", default=None, help="Specific task_id")
    p_res.add_argument("--refresh", action="store_true", help="Fetch remote baselines to detect merged Tasks")

    # skills
    p_sk = sub.add_parser("skills", help="Sync or verify single-source Companion Skill across Agent platforms")
    sk_sub = p_sk.add_subparsers(dest="subcommand", required=True)
    p_sk_sync = sk_sub.add_parser("sync", help="Preview or sync skills-src to platform directories")
    p_sk_sync.add_argument("--apply", action="store_true", help="Write synced skills and update lock")
    sk_sub.add_parser("check", help="Check platform skill copies for missing files or drift")

    # req
    p_req = sub.add_parser("req", help="Manage Requirements and human sign-off gates")
    req_sub = p_req.add_subparsers(dest="subcommand", required=True)
    p_rn = req_sub.add_parser("new", help="Create a new Requirement and minimal Spec skeleton")
    p_rn.add_argument("req_id")
    p_rn.add_argument("--title", required=True)
    p_rn.add_argument("--kind", default="feature", choices=("feature", "bugfix"))
    p_rn.add_argument("--owner", default="")
    p_rn.add_argument("--external-ref", default="")

    p_ra = req_sub.add_parser("approve", help="Record human sign-off on a Requirement gate")
    p_ra.add_argument("req_id")
    p_ra.add_argument("--gate", required=True, choices=("brief", "design", "e2e-plan", "e2e-result", "release-plan"))

    p_rc = req_sub.add_parser("close", help="Close Requirement after Tasks, E2E, and Release gates pass")
    p_rc.add_argument("req_id")

    # task
    p_task = sub.add_parser("task", help="Manage multi-repo Tasks, Worktrees, Checks, OCR Review, MR, and Cleanup")
    t_sub = p_task.add_subparsers(dest="subcommand", required=True)

    p_tn = t_sub.add_parser("new", help="Create a Task directory (`tasks/<tid>/`) inside a Requirement")
    p_tn.add_argument("req_id")
    p_tn.add_argument("task_id")
    p_tn.add_argument("--title", required=True)
    p_tn.add_argument("--repos", required=True, help="Comma-separated repo_ids")
    p_tn.add_argument("--assignee", default="")
    p_tn.add_argument("--contract", action="append", default=[])
    p_tn.add_argument("--depends-on", action="append", default=[])
    p_tn.add_argument("--verify", action="append", default=[], help="Format: repo_id:command")
    p_tn.add_argument("--context", action="append", default=[])

    p_ts = t_sub.add_parser("start", help="Validate gates and create isolated business repo Worktrees")
    p_ts.add_argument("req_id")
    p_ts.add_argument("task_id")
    p_ts.add_argument("--dry-run", action="store_true")

    p_tc = t_sub.add_parser("check", help="Run commit-bound multi-repo verification commands in parallel")
    p_tc.add_argument("req_id")
    p_tc.add_argument("task_id")
    p_tc.add_argument("--force", action="store_true", help="Disable evidence reuse for current commit")

    p_tr = t_sub.add_parser("review", help="Run external Code Review (default: Alibaba ocr CLI) or record human approval")
    p_tr.add_argument("req_id")
    p_tr.add_argument("task_id")
    p_tr.add_argument("--approve", action="store_true", help="Record human Code Review sign-off (Gate #3)")

    p_tm = t_sub.add_parser("mr", help="Verify check + review gates and emit target-explicit MR handoff info")
    p_tm.add_argument("req_id")
    p_tm.add_argument("task_id")

    p_tf = t_sub.add_parser("finish", help="Verify remote baseline merge ancestry and mark Task done")
    p_tf.add_argument("req_id")
    p_tf.add_argument("task_id")

    p_tcl = t_sub.add_parser("cleanup", help="Preview or safely remove clean Worktrees of a finished/cancelled Task")
    p_tcl.add_argument("req_id")
    p_tcl.add_argument("task_id")
    p_tcl.add_argument("--apply", action="store_true")

    # e2e
    p_e2e = sub.add_parser("e2e", help="Run optional E2E hook (supports both in-branch and post-merge testing)")
    e2e_sub = p_e2e.add_subparsers(dest="subcommand", required=True)
    p_e2e_run = e2e_sub.add_parser("run", help="Preview or execute hooks.e2e")
    p_e2e_run.add_argument("req_id")
    p_e2e_run.add_argument("--apply", action="store_true")

    # release
    p_rel = sub.add_parser("release", help="Generate release-plan.md or invoke optional QA/PRE release hook")
    rel_sub = p_rel.add_subparsers(dest="subcommand", required=True)
    p_rp = rel_sub.add_parser("plan", help="Generate/refresh release-plan.md from verified merged commits")
    p_rp.add_argument("req_id")
    p_rr = rel_sub.add_parser("run", help="Preview or run QA/PRE release hook (PROD is strictly manual-only)")
    p_rr.add_argument("req_id")
    p_rr.add_argument("--env", required=True, choices=("qa", "pre", "prod"))
    p_rr.add_argument("--apply", action="store_true")

    return parser


def _emit(json_mode: bool, command: str, data: Any, human_lines: list[str], *, ok: bool = True, errors: list[dict[str, Any]] | None = None) -> None:
    if json_mode:
        payload = {
            "ok": ok,
            "command": command,
            "data": data,
            "warnings": [],
            "errors": errors or [],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for line in human_lines:
        print(line)


def _dispatch(args: argparse.Namespace, workspace_arg: str | None) -> tuple[str, Any, list[str], int]:
    cmd = args.command
    if cmd == "init":
        target = Path(args.target).expanduser().resolve()
        repos = [_parse_repo_spec(r) for r in args.repo]
        platforms = tuple(args.platform) if args.platform else ("codex", "claude", "cursor")
        files = init_workspace(
            target,
            workspace_id=args.workspace_id,
            name=args.name,
            repositories=repos,
            baseline=args.baseline,
            platforms=platforms,
            dry_run=args.dry_run,
        )
        lines = [f"{'PREVIEW' if args.dry_run else 'CREATED'} {f}" for f in files]
        if args.dry_run:
            lines.append("Run again without --dry-run to write these files.")
        else:
            lines.append(f"Initialized amw-lite workspace `{args.workspace_id}` at {target}")
        return "init", {"target": str(target), "dry_run": args.dry_run, "files": files}, lines, EXIT_OK

    root = find_workspace_root(Path(workspace_arg) if workspace_arg else None)

    if cmd == "doctor":
        diags = run_doctor(root)
        has_err = any(d.severity == "ERROR" for d in diags)
        lines = [f"{d.severity} {d.code}{(' ' + d.target) if d.target else ''}: {d.message}" for d in diags]
        return "doctor", {"diagnostics": [d.to_dict() for d in diags]}, lines, (EXIT_VALIDATION_ERROR if has_err else EXIT_OK)

    if cmd == "resume":
        view = build_resume_view(root, req_id=args.req_id, task_id=args.task_id, refresh_remote=args.refresh)
        lines = [f"WORKSPACE {view['workspace_id']}"]
        if view.get("requirement_id"):
            lines.append(f"REQ {view['requirement_id']} [{view['requirement_status']}] {view['requirement_title']}")
            for t in view.get("tasks", []):
                blk = f" blockers={'; '.join(t['blockers'])}" if t["blockers"] else ""
                lines.append(f"  TASK {t['task_id']} status={t['status']} stage={t['effective_stage']}{blk}")
        nxt = view["next"]
        lines.append(f"NEXT {nxt['command'] or nxt['action']} — {nxt['reason']}")
        for alt in nxt.get("alternatives", []):
            lines.append(f"  ALT {alt}")
        return "resume", view, lines, EXIT_OK

    if cmd == "skills":
        if args.subcommand == "sync":
            actions = sync_skills(root, apply=args.apply)
            lines = [f"{'SYNCED' if args.apply else 'PLAN'} {a.skill} -> {a.target.relative_to(root)}" for a in actions]
            if not actions:
                lines = ["OK skills already in sync"]
            elif not args.apply:
                lines.append("Run again with --apply to write synced skills.")
            data = {"applied": args.apply, "actions": [{"skill": a.skill, "platform": a.platform, "target": str(a.target.relative_to(root))} for a in actions]}
            return "skills.sync", data, lines, EXIT_OK
        issues = check_skills(root)
        lines = [f"ERROR {i.code} {i.skill}: {i.message}" for i in issues] or ["OK all platform skills match skills-src"]
        return "skills.check", {"issues": [{"code": i.code, "skill": i.skill, "message": i.message} for i in issues]}, lines, (EXIT_VALIDATION_ERROR if issues else EXIT_OK)

    if cmd == "req":
        if args.subcommand == "new":
            req = create_requirement(root, args.req_id, args.title, kind=args.kind, owner=args.owner, external_ref=args.external_ref)
            return "req.new", req.to_dict(), [f"CREATED requirement {req.requirement_id} at requirements/{req.requirement_id}/"], EXIT_OK
        if args.subcommand == "approve":
            req = approve_requirement_gate(root, args.req_id, args.gate)
            return "req.approve", req.to_dict(), [f"APPROVED {req.requirement_id} gate={args.gate}"], EXIT_OK
        if args.subcommand == "close":
            res = close_requirement(root, args.req_id)
            lines = [f"CLOSED requirement {args.req_id}", f"KNOWLEDGE {res['knowledge_prompt']}"]
            for s in res["cleanup_suggestions"]:
                lines.append(f"CLEANUP {s}")
            return "req.close", res, lines, EXIT_OK

    if cmd == "task":
        if args.subcommand == "new":
            repos = tuple(r.strip() for r in args.repos.split(",") if r.strip())
            deps = tuple(_parse_dep_spec(d) for d in args.depends_on)
            verifies = tuple(_parse_verify_spec(v) for v in args.verify)
            task = create_task(
                root,
                args.req_id,
                args.task_id,
                args.title,
                repos,
                assignee=args.assignee,
                contracts=tuple(args.contract),
                depends_on=deps,
                verify_commands=verifies,
                context_files=tuple(args.context),
            )
            return "task.new", task.to_dict(), [f"CREATED task {task.requirement_id}/{task.task_id} repos={','.join(task.repositories)}"], EXIT_OK
        if args.subcommand == "start":
            plans = start_task(root, args.req_id, args.task_id, dry_run=args.dry_run)
            lines = [
                f"{'PLAN' if args.dry_run else 'CREATED'} {p.repo_id} branch={p.branch} baseline={p.baseline}@{p.baseline_commit[:10]} path={p.target}"
                for p in plans
            ]
            data = {
                "dry_run": args.dry_run,
                "worktrees": [
                    {"repo_id": p.repo_id, "branch": p.branch, "baseline": p.baseline, "baseline_commit": p.baseline_commit, "target": str(p.target)}
                    for p in plans
                ],
            }
            return "task.start", data, lines, EXIT_OK
        if args.subcommand == "check":
            task = load_task(root, args.req_id, args.task_id)
            report = run_task_check(root, task, force=args.force)
            lines = [
                f"{'REUSE' if o.reused else 'PASS'} {o.repo_id} {o.commit[:10]} :: {o.command}"
                for o in report.outcomes
            ] or [f"PASS {args.req_id}/{args.task_id} (no verify commands configured)"]
            data = {
                "passed": report.passed,
                "outcomes": [
                    {"repo_id": o.repo_id, "command": o.command, "commit": o.commit, "exit_code": o.exit_code, "reused": o.reused}
                    for o in report.outcomes
                ],
            }
            return "task.check", data, lines, EXIT_OK
        if args.subcommand == "review":
            task = load_task(root, args.req_id, args.task_id)
            summaries = run_task_review(root, task, approve=args.approve)
            lines = [
                f"{'APPROVED' if s.human_approved else 'REVIEWED'} {s.repo_id} tool={s.tool} commit={s.commit[:10]} findings={s.findings_count} ({s.report_path})"
                for s in summaries
            ]
            data = {
                "approved": args.approve,
                "reviews": [
                    {
                        "repo_id": s.repo_id,
                        "tool": s.tool,
                        "commit": s.commit,
                        "findings_count": s.findings_count,
                        "human_approved": s.human_approved,
                        "report_path": s.report_path,
                    }
                    for s in summaries
                ],
            }
            return "task.review", data, lines, EXIT_OK
        if args.subcommand == "mr":
            plans = build_task_mr_plans(root, args.req_id, args.task_id)
            lines = []
            for p in plans:
                lines.append(f"MR {p.repo_id} {p.source_branch} -> {p.target_branch} commit={p.commit[:10]}")
                lines.append(f"  PUSH {p.push_command}")
                if p.url:
                    lines.append(f"  URL  {p.url}")
            return "task.mr", {"merge_requests": [p.to_dict() for p in plans]}, lines, EXIT_OK
        if args.subcommand == "finish":
            task = finish_task(root, args.req_id, args.task_id)
            return "task.finish", task.to_dict(), [f"DONE {task.requirement_id}/{task.task_id}"], EXIT_OK
        if args.subcommand == "cleanup":
            task = load_task(root, args.req_id, args.task_id)
            items = plan_cleanup_worktrees(root, task)
            if args.apply:
                apply_cleanup_worktrees(items)
            lines = [f"{'REMOVED' if args.apply else 'PLAN_REMOVE'} {i.repo_id} {i.target} (branch {i.branch} preserved)" for i in items]
            if not items:
                lines = ["OK no worktrees to remove"]
            elif not args.apply:
                lines.append("Run again with --apply to remove these clean worktrees.")
            data = {"applied": args.apply, "items": [{"repo_id": i.repo_id, "target": str(i.target), "branch": i.branch} for i in items]}
            return "task.cleanup", data, lines, EXIT_OK

    if cmd == "e2e" and args.subcommand == "run":
        preview = run_e2e_hook(root, args.req_id, apply=args.apply)
        lines = [f"E2E {'APPLIED' if preview.applied else 'PREVIEW'} {' '.join(preview.argv) or '(manual)'}", preview.note]
        return "e2e.run", {"applied": preview.applied, "argv": list(preview.argv), "exit_code": preview.exit_code, "note": preview.note}, lines, EXIT_OK

    if cmd == "release":
        if args.subcommand == "plan":
            out = generate_release_plan(root, args.req_id)
            return "release.plan", {"path": str(out.relative_to(root))}, [f"GENERATED {out.relative_to(root)}"], EXIT_OK
        if args.subcommand == "run":
            preview = run_release_hook(root, args.req_id, args.env, apply=args.apply)
            lines = [f"RELEASE({args.env}) {'APPLIED' if preview.applied else 'PREVIEW'} {' '.join(preview.argv) or '(manual)'}", preview.note]
            return "release.run", {"env": args.env, "applied": preview.applied, "argv": list(preview.argv), "exit_code": preview.exit_code, "note": preview.note}, lines, EXIT_OK

    raise AmwLiteError(f"unsupported command: {cmd}", code="UNSUPPORTED_COMMAND", exit_code=EXIT_VALIDATION_ERROR)


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    cleaned, json_mode, workspace_arg = _extract_global_flags(raw)
    parser = _build_parser()
    try:
        args = parser.parse_args(cleaned)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_VALIDATION_ERROR

    try:
        command_name, data, lines, exit_code = _dispatch(args, workspace_arg)
    except AmwLiteError as exc:
        err_entry = {"code": exc.code, "message": exc.message, "details": exc.details}
        if json_mode:
            _emit(True, args.command, None, [], ok=False, errors=[err_entry])
        else:
            print(f"ERROR [{exc.code}]: {exc.message}", file=sys.stderr)
        return exc.exit_code

    _emit(json_mode, command_name, data, lines, ok=(exit_code == EXIT_OK))
    return exit_code
