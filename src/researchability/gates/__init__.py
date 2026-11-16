"""Automated gate evaluators across Stages 0-5."""

from researchability.gates.base import EvaluationContext, LLMGateVerdict
from researchability.gates.runner import (
    ALL_STAGE0_TO_3_EVALUATORS,
    ALL_STAGE0_TO_5_EVALUATORS,
    evaluate_stage0_to_3,
    evaluate_stage0_to_5,
    evaluate_stage0_to_5_with_context,
)
from researchability.gates.stage5_verification import NumericComparison, render_human_dossier

__all__ = [
    "ALL_STAGE0_TO_3_EVALUATORS",
    "ALL_STAGE0_TO_5_EVALUATORS",
    "EvaluationContext",
    "LLMGateVerdict",
    "NumericComparison",
    "evaluate_stage0_to_3",
    "evaluate_stage0_to_5",
    "evaluate_stage0_to_5_with_context",
    "render_human_dossier",
]
