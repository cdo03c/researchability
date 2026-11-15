"""Stage 0-3 evaluation runner producing a full AuditReport."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from researchability.gates.base import EvaluationContext
from researchability.gates.stage0_1 import STAGE0_1_EVALUATORS
from researchability.gates.stage2 import STAGE2_EVALUATORS
from researchability.gates.stage3_design_stats import STAGE3_DESIGN_STATS_EVALUATORS
from researchability.gates.stage3_integrity import STAGE3_INTEGRITY_EVALUATORS
from researchability.llm.client import GeminiClient
from researchability.models import AuditReport, FieldProfile, GateResult, PaperBundle
from researchability.registry import GateRegistry
from researchability.scoring import compute_audit_report

GateCallable = Callable[[EvaluationContext], GateResult]

ALL_STAGE0_TO_3_EVALUATORS: dict[str, GateCallable] = {
    **STAGE0_1_EVALUATORS,
    **STAGE2_EVALUATORS,
    **STAGE3_DESIGN_STATS_EVALUATORS,
    **STAGE3_INTEGRITY_EVALUATORS,
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
