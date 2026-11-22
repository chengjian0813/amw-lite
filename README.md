# amw-lite

> **Lightweight, open-source Multi-repo AI Agent Workspace** for 2–5 person microservice teams using Claude Code, Codex, or Cursor.

`amw-lite` gives small backend teams a clean Git control repository, isolated multi-repo Git Worktrees, minimal native Markdown Specs, commit-bound verification, and pluggable external CLI hooks (including Alibaba's open-source [`ocr`](https://github.com/alibaba/open-code-review) Code Review CLI) — all in **under 4,000 lines of dependency-light Python** (MIT License, only `PyYAML`).

---

## Why `amw-lite`?

When a small team modifies multiple microservices together with mixed Coding Agents (Claude Code / Codex / Cursor), two extremes usually fail:
- **Zero structure**: Agents pollute daily checkouts, guess cross-repo APIs, skip tests after last-minute edits, and drift across tools.
- **Over-engineered platforms**: Heavy frameworks, nested control-repo worktrees, local SQLite projectors, mandatory waiver bureaucracy, and AGPL workflow engines create huge onboarding friction and over-constrain modern LLMs.

`amw-lite` takes the pragmatic middle path:
- **Focus humans strictly on 6 high-leverage decision gates**:
  1. Requirement understanding (`brief.md`)
  2. System design & cross-repo contracts (`design.md` + `contracts/*.md`)
  3. Code Review (`amw-lite task review` via Alibaba `ocr` CLI + human sign-off)
  4. E2E test case design (`e2e-plan.md`)
  5. E2E test execution results
  6. Release plan (`release-plan.md`) + 1–3 distilled learnings for `context/`
- **Hard-code only 4 physical safety boundaries**:
  1. Source checkouts stay read-only; business edits happen in `.worktrees/<REQ>/<TASK>/<REPO>/`
  2. Verification & Review evidence are bound to exact `HEAD commit + pre/post clean`
  3. Static secret / local-path / unresolved-placeholder scanning
  4. Never auto-push, auto-merge MRs, or auto-deploy PROD

---

## 10-Minute Quick Start

Requirements: **Python 3.10+** and **Git**.

```bash
# Install with uv (recommended) or pipx / pip
uv tool install git+https://github.com/chengjian0813/amw-lite.git
# or from local clone:
uv tool install .

amw-lite --version
```

### 1. Initialize a Workspace Control Repository

```bash
mkdir order-workspace && cd order-workspace
git init -b main

amw-lite init \
  --id order-domain \
  --name "Order Domain Workspace" \
  --repo order-core=/abs/path/to/order-core \
  --repo order-biz=/abs/path/to/order-biz

amw-lite doctor
```

This creates:
- `.workspace/workspace.yaml` & `repositories.yaml` (committed, logical definitions)
- `.workspace/local.yaml` (gitignored, machine-local paths)
- `AGENTS.md`, `CLAUDE.md`, and synced Companion Skill in `.agents/`, `.claude/`, `.cursor/`

### 2. Drive the Workflow via Natural Language or CLI

Open Claude Code, Codex, or Cursor in `order-workspace`:
> Use the `amw-lite` skill: run `amw-lite resume --json` and guide me through the next step.

Or run the CLI directly:

```bash
# 1. Create requirement & pass Gates #1 and #2
amw-lite req new REQ-001 --title "Multi-currency checkout"
# (fill brief.md, design.md, and mark contracts/*.md status: accepted)
amw-lite req approve REQ-001 --gate brief
amw-lite req approve REQ-001 --gate design

# 2. Split tasks & start isolated multi-repo worktrees
amw-lite task new REQ-001 T01 --repos order-core,order-biz --title "Core + Biz checkout"
# (fill requirements/REQ-001/tasks/T01/plan.md)
amw-lite task start REQ-001 T01

# 3. Code in .worktrees/REQ-001/T01/<repo>, commit, check & Code Review (Gate #3)
amw-lite task check REQ-001 T01
amw-lite task review REQ-001 T01          # runs Alibaba `ocr review` (or diff-stat fallback)
# Fix any CR comments directly in the current Task branch, commit, re-run check & review!
amw-lite task review REQ-001 T01 --approve

# 4. E2E testing in current branch (Gates #4 & #5) — also supports post-merge integration E2E
amw-lite req approve REQ-001 --gate e2e-plan
amw-lite e2e run REQ-001 --apply          # optional external E2E hook
# Fix E2E bugs directly in the current branch, re-run check/review/e2e!
amw-lite req approve REQ-001 --gate e2e-result

# 5. Merge MR, finish Task, generate Release Plan (Gate #6), distill knowledge & cleanup
amw-lite task mr REQ-001 T01              # prints exact push command & MR URL
# (human pushes & merges business MR)
amw-lite task finish REQ-001 T01
amw-lite release plan REQ-001
amw-lite req approve REQ-001 --gate release-plan
amw-lite req close REQ-001
amw-lite task cleanup REQ-001 T01 --apply
```

---

## Code Review Integration (Alibaba Open Code Review `ocr`)

`amw-lite` does not reinvent AI Code Review. Out of the box, `.workspace/workspace.yaml` wires `hooks.review` to Alibaba's open-source [`ocr`](https://github.com/alibaba/open-code-review) CLI (Apache-2.0):

```bash
npm install -g @alibaba-group/open-code-review
```

When you run `amw-lite task review <REQ> <TASK>`, `amw-lite` automatically executes per repository worktree:

```bash
ocr review --from <baseline_commit> --to <head_commit> --background "<brief+design summary>" --audience agent --format json
```

- If `ocr` is not installed, `amw-lite` gracefully falls back to `git diff --stat` so you can still review manually and run `--approve`.
- Every review record is bound to the exact `HEAD` commit — fixing a bug in the current branch automatically invalidates stale check/review proofs for modified repos while reusing passing checks for untouched repos.

---

## Pluggable External Hooks (`hooks.e2e` / `hooks.release`)

Configure any team-specific E2E runner or fast deployment CLI in `.workspace/workspace.yaml` (or machine-local overrides in `.workspace/local.yaml`) using strict `argv` arrays (zero shell injection):

```yaml
hooks:
  e2e:
    enabled: true
    argv: ["my-e2e-cli", "run", "--plan", "{e2e_plan_path}"]
  release:
    enabled: true
    policy:
      qa: ask
      pre: ask
      prod: manual   # PROD auto-deploy is permanently blocked
    argv: ["my-deploy-cli", "deploy", "--plan", "{release_plan_path}", "--env", "{env}"]
```

---

## Command Reference

All commands support `--json` (before or after the subcommand) for reliable Agent parsing.

| Command | Purpose |
|---|---|
| `amw-lite init` | Scaffold a new Workspace control repo and sync Companion Skill |
| `amw-lite doctor` | Read-only check of Git, local repo remotes, skills, hooks, DAGs, and secrets |
| `amw-lite resume [--refresh]` | Show current status and single deterministic `NEXT` action |
| `amw-lite skills sync [--apply] \| check` | Single-source sync & drift check across Codex / Claude / Cursor |
| `amw-lite req new \| approve \| close` | Manage Requirement specs and human decision gates |
| `amw-lite task new \| start \| check \| review \| mr \| finish \| cleanup` | Full multi-repo Task & Worktree lifecycle |
| `amw-lite e2e run <REQ> [--apply]` | Preview or invoke configured E2E hook |
| `amw-lite release plan \| run` | Generate verified `release-plan.md` or run QA/PRE release hook |

---

## License

MIT License — see [LICENSE](LICENSE).
