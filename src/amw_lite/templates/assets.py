"""Minimal, human-readable templates bundled with amw-lite."""

from __future__ import annotations

COMPANION_SKILL_MD = """---
name: amw-lite
description: Operate a multi-repository amw-lite Workspace through natural language while honoring the 6 human decision gates and 4 physical safety boundaries.
---

# amw-lite Workspace Operator

Translate the user's intent into safe `amw-lite` CLI commands. Keep ceremony minimal and focus human attention strictly on high-leverage decisions.

## 1. Always start from objective facts
1. Run `amw-lite resume --json` to inspect current Requirement, Task EffectiveStage, blockers, and the single recommended `NEXT`.
2. Run `amw-lite <command> --help` before constructing unfamiliar arguments; never guess flags from memory.
3. Never hand-edit `execution` or `evidence` fields inside `task.yaml`.

## 2. Honor the 6 human decision gates (ALWAYS pause for explicit sign-off)
1. **Requirement understanding (`brief.md`)**: Draft `brief.md` after reading source repos read-only; stop until the user approves, then run `amw-lite req approve <REQ> --gate brief`.
2. **System design & Contracts (`design.md` + `contracts/*.md`)**: Draft system design and cross-repo contracts; stop until the user approves design and marks required contracts `status: accepted`.
3. **Code Review (`amw-lite task review`)**: After `amw-lite task check` passes, run `amw-lite task review <REQ> <TASK>` (which invokes Alibaba `ocr` CLI if installed, or diff-stat fallback). Present findings and stop for human Code Review. If changes are requested, **fix directly in the current Task branch and worktree** (never create a new branch), re-commit, and re-run `check` + `review` before `--approve`.
4. **E2E case design (`e2e-plan.md`)**: Draft concrete scenarios and assertions; stop for human approval (`--gate e2e-plan`).
5. **E2E execution result**: Run E2E (via `amw-lite e2e run` or guided execution). Fix bugs **directly in the current Task branch**, re-verify, and present real evidence for human approval (`--gate e2e-result`).
6. **Release plan (`release-plan.md`)**: Generate `amw-lite release plan <REQ>`; present exact commits, order, and rollback notes for human approval (`--gate release-plan`).

## 3. Automate the mechanical middle
- Once Gate #2 (design & contracts) is approved, proceed smoothly through `task new` -> `task start` (create isolated worktrees) -> implement code & unit tests -> `task check` -> `task review` without interrupting the user on each local step.
- After Gate #6 (release plan) and before `amw-lite req close <REQ>`, compare `design.md` with actual diffs/fixes and propose **1-3 concise durable learnings**. Ask the user: *"Keep which numbers, or skip all?"* Append chosen items directly to `context/`.

## 4. Never violate the 4 safety boundaries
- Only modify business code inside `.worktrees/<REQ>/<TASK>/<REPO>/`; treat source checkouts as strictly read-only.
- Never run `git push` or create MR without explicit confirmation; **MR merge and PROD deployment are always human-owned**.
- Preview before applying `task cleanup --apply`, `skills sync --apply`, `e2e run --apply`, or `release run --env qa|pre --apply`.
- Never write tokens, passwords, private keys, or machine-local absolute paths into Git-tracked files.
"""

AGENTS_MD_TEMPLATE = """# {name} — Agent Guide

This is an `amw-lite` multi-repository Workspace control repository.

## Quick Orientation
- **Start here**: Run `amw-lite resume --json` (and `amw-lite doctor` if environment looks unclear).
- **Companion Skill**: Follow `.workspace/skills-src/amw-lite/SKILL.md` (synced to `.agents/`, `.claude/`, `.cursor/`).
- **Repositories**: Logical definitions live in `.workspace/repositories.yaml`; machine-local checkout paths live in gitignored `.workspace/local.yaml`.
- **Long-term context**: Read relevant files in `context/` and accepted cross-repo contracts in `contracts/`.
- **Active work**: Each requirement lives under `requirements/<REQ-ID>/`; each Task owns its own `tasks/<TASK-ID>/` folder and isolated business worktrees under `.worktrees/<REQ-ID>/<TASK-ID>/<repo>/`.

## Rules
1. Never edit business repositories directly in their source checkouts; work only inside `.worktrees/`.
2. Pause for explicit human sign-off at all 6 decision gates: Brief, Design/Contracts, Code Review (`ocr` + human), E2E plan, E2E result, and Release plan.
3. Fix Code Review and E2E issues directly in the current Task branch; never spin up extra fix branches before merge.
4. Never commit secrets, tokens, or local absolute paths. Never auto-merge MRs or auto-deploy PROD.
"""

CLAUDE_MD_TEMPLATE = """# {name} — Claude Guide

Read and follow [AGENTS.md](AGENTS.md) and the bundled `amw-lite` Skill (`.claude/skills/amw-lite/SKILL.md`).
Always start with `amw-lite resume --json`.
"""

WORKSPACE_README_TEMPLATE = """# {name}

Multi-repository AI Agent Workspace managed by [`amw-lite`](https://github.com/chengjian0813/amw-lite).

## 10-Minute Onboarding
1. Install `amw-lite`: `uv tool install git+https://github.com/chengjian0813/amw-lite.git` (or `pipx install git+https://github.com/chengjian0813/amw-lite.git`).
2. Copy `.workspace/local.example.yaml` to `.workspace/local.yaml` and fill in your local repository checkout paths.
3. Run `amw-lite doctor` to verify Git, repositories, and optional tools (such as Alibaba `ocr` CLI).
4. Open Claude Code, Codex, or Cursor in this directory and say:
   > Use the `amw-lite` skill to check `resume` and guide me through the next step.
"""

WORKSPACE_GITIGNORE = """.workspace/local.yaml
.workspace/runtime/
.worktrees/
.DS_Store
"""

DOMAIN_CONTEXT_TEMPLATE = """# Domain Context

Record stable domain glossary, core state machines, and cross-repository invariants here.
At the end of each requirement, `amw-lite` prompts to distill 1-3 durable learnings into this folder.
"""

BRIEF_MD_TEMPLATE = """# Requirement Brief: {req_id} — {title}

## Background & Goal
- TBD

## Scope & Non-Goals
- In scope:
- Out of scope:

## Acceptance Criteria
- [ ] TBD
"""

DESIGN_MD_TEMPLATE = """# System Design: {req_id} — {title}

## Architecture & Affected Repositories
- TBD

## Cross-Repository Contracts & Data Flow
- Reference accepted contracts in `contracts/` before starting parallel tasks.

## Key Invariants, Idempotency & Failure Handling
- TBD
"""

TASK_PLAN_MD_TEMPLATE = """# Task Plan: {req_id} / {task_id} — {title}

## Design Reference
- `requirements/{req_id}/design.md`

## Implementation Steps
1. TBD

## Verification Plan
- Unit tests and commands declared in `task.yaml`.
"""

E2E_PLAN_MD_TEMPLATE = """# E2E Test Plan: {req_id} — {title}

## Test Cases (Human Gate #4)
- [ ] Happy path: TBD
- [ ] Boundary / failure case: TBD

## Execution Results (Human Gate #5)
- Environment:
- Observed Outcome:
"""

RELEASE_PLAN_MD_TEMPLATE = """# Release Plan: {req_id} — {title}

Run `amw-lite release plan {req_id}` once Tasks are merged to populate exact commits and deployment order.
"""
