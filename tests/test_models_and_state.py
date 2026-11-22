"""Unit tests for models, dependency DAG, contract parallel gate, and EffectiveStage derivation."""

from __future__ import annotations

import pytest

from amw_lite.errors import AmwLiteError
from amw_lite.models import (
    CheckEvidence,
    HookConfig,
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
from amw_lite.state import (
    EffectiveStage,
    RepoWorktreeObservation,
    derive_next_action,
    derive_task_stage,
    detect_dependency_cycle,
    evaluate_ready_blockers,
    topological_task_order,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def test_workspace_rejects_prod_auto_policy() -> None:
    with pytest.raises(AmwLiteError, match="invalid release policy for prod"):
        WorkspaceConfig(
            workspace_id="order-domain",
            name="Order",
            hooks={"release": HookConfig(enabled=True, policy={"prod": "auto"})},
        )


def test_dependency_cycle_and_topological_order() -> None:
    t1 = Task("REQ-1", "T01", "First", ("order-core",), depends_on=(TaskDependency("T02", "hard_code"),))
    t2 = Task("REQ-1", "T02", "Second", ("order-biz",), depends_on=(TaskDependency("T01", "hard_code"),))
    cycle = detect_dependency_cycle({"T01": t1, "T02": t2})
    assert cycle == ["T01", "T02", "T01"]

    t_a = Task("REQ-1", "T01", "Core", ("order-core",))
    t_b = Task("REQ-1", "T02", "Biz", ("order-biz",), depends_on=(TaskDependency("T01", "hard_code"),))
    assert topological_task_order({"T02": t_b, "T01": t_a}) == ("T01", "T02")


def test_hard_code_vs_contract_dependency_parallelism() -> None:
    upstream = Task("REQ-1", "T01", "Core API", ("order-core",), status="active")
    hard_down = Task(
        "REQ-1",
        "T02",
        "Hard downstream",
        ("order-biz",),
        depends_on=(TaskDependency("T01", "hard_code"),),
    )
    contract_down = Task(
        "REQ-1",
        "T03",
        "Contract downstream",
        ("order-biz",),
        contracts=("contracts/api.md",),
        depends_on=(TaskDependency("T01", "contract", contract="contracts/api.md"),),
    )
    all_tasks = {"T01": upstream, "T02": hard_down, "T03": contract_down}
    known = {"order-core", "order-biz"}

    hard_blockers = evaluate_ready_blockers(hard_down, all_tasks, known, lambda _: "accepted")
    assert any("HARD_DEPENDENCY_NOT_DONE" in b for b in hard_blockers)

    draft_contract_blockers = evaluate_ready_blockers(contract_down, all_tasks, known, lambda _: "draft")
    assert any("CONTRACT_NOT_ACCEPTED" in b for b in draft_contract_blockers)

    accepted_contract_blockers = evaluate_ready_blockers(contract_down, all_tasks, known, lambda _: "accepted")
    assert accepted_contract_blockers == ()


def test_effective_stage_and_commit_invalidation() -> None:
    repos = {"order-core": RepositoryConfig("order-core", "Core", default_verify=("pytest",))}
    base_task = Task(
        requirement_id="REQ-1",
        task_id="T01",
        title="Implement API",
        repositories=("order-core",),
        status="active",
        verify_commands=(VerifyCommand("order-core", "pytest"),),
        executions={"order-core": RepoExecution("amw/REQ-1/T01", "main", SHA_A)},
        checks=(CheckEvidence("order-core", "pytest", SHA_B, 0, True, True, "2026-11-22T10:00:00Z"),),
        reviews=(ReviewEvidence("order-core", "ocr", SHA_B, 0, True, "2026-11-22T10:05:00Z"),),
    )

    # At SHA_B with passing check + approved review -> REVIEWED
    obs_b = {"order-core": RepoWorktreeObservation("order-core", True, True, "amw/REQ-1/T01", SHA_B, False)}
    st = derive_task_stage(base_task, {"T01": base_task}, repos, lambda _: "accepted", obs_b)
    assert st.stage == EffectiveStage.REVIEWED

    # After fixing a bug in the same branch (new commit SHA_C) -> stale check/review auto-invalidated -> DEVELOPING
    obs_c = {"order-core": RepoWorktreeObservation("order-core", True, True, "amw/REQ-1/T01", SHA_C, False)}
    st_after_fix = derive_task_stage(base_task, {"T01": base_task}, repos, lambda _: "accepted", obs_c)
    assert st_after_fix.stage == EffectiveStage.DEVELOPING

    # When merged at SHA_B -> MERGED
    obs_merged = {"order-core": RepoWorktreeObservation("order-core", True, True, "amw/REQ-1/T01", SHA_B, True)}
    st_merged = derive_task_stage(base_task, {"T01": base_task}, repos, lambda _: "accepted", obs_merged)
    assert st_merged.stage == EffectiveStage.MERGED


def test_derive_next_action_in_branch_e2e_with_mr_alternative() -> None:
    req = Requirement(
        "REQ-1",
        "Checkout",
        gates=RequirementGates(brief_approved=True, design_approved=True),
    )
    repos = {"order-core": RepositoryConfig("order-core", "Core")}
    task = Task(
        "REQ-1",
        "T01",
        "Core",
        ("order-core",),
        status="active",
        executions={"order-core": RepoExecution("amw/REQ-1/T01", "main", SHA_A)},
        reviews=(ReviewEvidence("order-core", "ocr", SHA_B, 0, True, "2026-11-22T10:05:00Z"),),
    )
    obs = {"order-core": RepoWorktreeObservation("order-core", True, True, "amw/REQ-1/T01", SHA_B, False)}
    sr = derive_task_stage(task, {"T01": task}, repos, lambda _: "accepted", obs)
    nxt = derive_next_action(req, (task,), {"T01": sr})
    assert nxt.action == "req.approve.e2e_plan"
    assert nxt.requires_human_gate == "e2e_plan"
    assert "amw-lite task mr REQ-1 T01" in nxt.alternatives
