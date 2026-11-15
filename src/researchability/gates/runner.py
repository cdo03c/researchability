"""Stage 0-5 evaluation runners producing AuditReports and human debate dossiers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from researchability.gates.base import EvaluationContext
from researchability.gates.stage0_1 import STAGE0_1_EVALUATORS
from researchability.gates.stage2 import STAGE2_EVALUATORS
from researchability.gates.stage3_design_stats import STAGE3_DESIGN_STATS_EVALUATORS
from researchability.gates.stage3_integrity import STAGE3_INTEGRITY_EVALUATORS
from researchability.gates.stage4_execution import STAGE4_EVALUATORS
from researchability.gates.stage5_verification import (
    STAGE5_EVALUATORS,
    render_human_dossier,
)
from researchability.gates.stage_crossfield import STAGE_CROSSFIELD_EVALUATORS
from researchability.llm.client import GeminiClient
from researchability.models import AuditReport, FieldProfile, GateResult, PaperBundle
from researchability.registry import GateRegistry
from researchability.sandbox import DockerSandboxRunner, SandboxConfig
from researchability.scoring import compute_audit_report, resolve_dependency_override

GateCallable = Callable[[EvaluationContext], GateResult]

ALL_STAGE0_TO_3_EVALUATORS: dict[str, GateCallable] = {
    **STAGE0_1_EVALUATORS,
    **STAGE2_EVALUATORS,
    **STAGE3_DESIGN_STATS_EVALUATORS,
    **STAGE3_INTEGRITY_EVALUATORS,
    **STAGE_CROSSFIELD_EVALUATORS,
}

ALL_STAGE0_TO_5_EVALUATORS: dict[str, GateCallable] = {
    **ALL_STAGE0_TO_3_EVALUATORS,
    **STAGE4_EVALUATORS,
    **STAGE5_EVALUATORS,
}


def evaluate_stage0_to_3(
    paper: PaperBundle,
    registry: GateRegistry | None = None,
    *,
    workdir: Path | None = None,
    llm: GeminiClient | None = None,
    profile: FieldProfile | None = None,
) -> AuditReport:
    """Run all Stage 0-3 automated gate evaluators and compute an AuditReport."""
    active_registry = registry or GateRegistry.from_yaml()
    active_profile = profile or paper.primary_profile
    ctx = EvaluationContext(paper=paper, workdir=workdir, llm=llm)

    raw_results: dict[str, GateResult] = {}
    for gate in active_registry.gates_for_profile(active_profile):
        evaluator = ALL_STAGE0_TO_3_EVALUATORS.get(gate.id)
        if evaluator is not None:
            raw_results[gate.id] = evaluator(ctx)

    return compute_audit_report(
        paper=paper,
        registry=active_registry,
        raw_results=raw_results,
        profile=active_profile,
    )


def evaluate_stage0_to_5_with_context(
    paper: PaperBundle,
    registry: GateRegistry | None = None,
    *,
    workdir: Path | None = None,
    llm: GeminiClient | None = None,
    profile: FieldProfile | None = None,
    sandbox: DockerSandboxRunner | None = None,
    sandbox_config: SandboxConfig | None = None,
) -> tuple[AuditReport, EvaluationContext]:
    """Run Stage 0-5 evaluators (sandbox execution & verification) and return (report, ctx)."""
    active_registry = registry or GateRegistry.from_yaml()
    active_profile = profile or paper.primary_profile
    ctx = EvaluationContext(paper=paper, workdir=workdir, llm=llm)
    ctx.extracted_claims["sandbox_runner"] = sandbox or DockerSandboxRunner(sandbox_config)

    resolved: dict[str, GateResult] = {}
    raw_results: dict[str, GateResult] = {}
    active_gates = active_registry.gates_for_profile(active_profile)

    for gate in active_gates:
        override = resolve_dependency_override(gate, resolved)
        if override is not None:
            resolved[gate.id] = override
            continue

        evaluator = ALL_STAGE0_TO_5_EVALUATORS.get(gate.id)
        if evaluator is None:
            continue

        if gate.id == "G5.3_HUMAN_DOSSIER_READY":
            ctx.extracted_claims["preliminary_results"] = list(resolved.values())

        result = evaluator(ctx)
        raw_results[gate.id] = result
        resolved[gate.id] = result

    report = compute_audit_report(
        paper=paper,
        registry=active_registry,
        raw_results=raw_results,
        profile=active_profile,
    )

    if resolved.get("G5.3_HUMAN_DOSSIER_READY") is not None:
        ctx.extracted_claims["human_dossier_markdown"] = render_human_dossier(
            report,
            paper=paper,
            comparisons=ctx.extracted_claims.get("numeric_comparisons"),
            repair_diffs=ctx.extracted_claims.get("repair_diffs"),
            regenerated_artifacts=ctx.extracted_claims.get("regenerated_artifacts"),
            robustness_checks=ctx.extracted_claims.get("suggested_robustness_checks"),
            hygiene_audit=ctx.extracted_claims.get("hygiene_audit"),
            dependency_audit=ctx.extracted_claims.get("dependency_audit"),
            execution_log_audit=ctx.extracted_claims.get("execution_log_audit"),
            table_figure_coverage=ctx.extracted_claims.get("table_figure_coverage"),
            quantitative_robustness=ctx.extracted_claims.get("quantitative_robustness"),
            distributional_verification=ctx.extracted_claims.get("distributional_verification"),
        )

    return report, ctx


def evaluate_stage0_to_5(
    paper: PaperBundle,
    registry: GateRegistry | None = None,
    *,
    workdir: Path | None = None,
    llm: GeminiClient | None = None,
    profile: FieldProfile | None = None,
    sandbox: DockerSandboxRunner | None = None,
    sandbox_config: SandboxConfig | None = None,
) -> AuditReport:
    """Run Stage 0-5 automated evaluators (with sandboxed execution) and return AuditReport."""
    report, _ = evaluate_stage0_to_5_with_context(
        paper=paper,
        registry=registry,
        workdir=workdir,
        llm=llm,
        profile=profile,
        sandbox=sandbox,
        sandbox_config=sandbox_config,
    )
    return report
