"""Tests for scoring aggregation, dependency propagation, and funnel depth calculation."""

from researchability.models import (
    FieldProfile,
    GateResult,
    GateStatus,
    PaperBundle,
    ProblemDomain,
)
from researchability.registry import GateRegistry
from researchability.scoring import compute_audit_report, render_markdown_summary


def _sample_paper() -> PaperBundle:
    return PaperBundle(
        paper_id="arxiv:2501.01234",
        source_repo="arxiv",
        title="Reproducible ML Study",
        published_year=2025,
        primary_profile=FieldProfile.ML_CS,
    )


def _pass_all(registry: GateRegistry, profile: FieldProfile) -> dict[str, GateResult]:
    return {
        gate.id: GateResult(gate_id=gate.id, status=GateStatus.PASS, rationale="ok")
        for gate in registry.gates_for_profile(profile)
    }


def test_all_passing_paper_reaches_funnel_depth_5() -> None:
    registry = GateRegistry.from_yaml()
    paper = _sample_paper()
    raw = _pass_all(registry, paper.primary_profile)

    report = compute_audit_report(paper, registry, raw)
    assert report.funnel_depth == 5
    assert report.composite_score == 1.0
    assert report.unweighted_pass_rate == 1.0
    assert report.zero_touch_reproducible is True
    assert report.repaired_reproducible is True
    assert report.counts["fail"] == 0
    assert report.counts["blocked"] == 0


def test_missing_code_propagates_na_without_multiplying_failures() -> None:
    registry = GateRegistry.from_yaml()
    paper = _sample_paper()
    raw = _pass_all(registry, paper.primary_profile)
    raw["G1.3_CODE_LINK_PRESENT"] = GateResult(
        gate_id="G1.3_CODE_LINK_PRESENT",
        status=GateStatus.FAIL,
        rationale="No code link found in manuscript.",
        flags=["No repository URL found"],
    )

    report = compute_audit_report(paper, registry, raw)
    by_id = {r.gate_id: r for r in report.gate_results}

    # Only G1.3 should be FAIL among code-dependent gates; downstream gates become N/A
    assert by_id["G1.3_CODE_LINK_PRESENT"].status == GateStatus.FAIL
    assert by_id["G1.4_CODE_NON_STUB"].status == GateStatus.N_A
    assert by_id["G2.1_README_ENTRYPOINT"].status == GateStatus.N_A
    assert by_id["G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH"].status == GateStatus.N_A
    assert by_id["G5.1_NUMERIC_OUTCOMES_MATCH"].status == GateStatus.N_A
    assert report.counts["fail"] == 1
    assert report.funnel_depth == 0
    assert report.zero_touch_reproducible is None
    assert "[G1.3_CODE_LINK_PRESENT] No repository URL found" in report.human_review_flags


def test_blocked_data_access_propagates_blocked_and_preserves_zero_fail() -> None:
    registry = GateRegistry.from_yaml()
    paper = _sample_paper()
    raw = _pass_all(registry, paper.primary_profile)
    raw["G1.2_DATA_ACCESSIBLE"] = GateResult(
        gate_id="G1.2_DATA_ACCESSIBLE",
        status=GateStatus.BLOCKED_ACCESS,
        rationale="Dataset requires IRB approval.",
    )

    report = compute_audit_report(paper, registry, raw)
    by_id = {r.gate_id: r for r in report.gate_results}

    assert by_id["G1.2_DATA_ACCESSIBLE"].status == GateStatus.BLOCKED_ACCESS
    assert by_id["G3.7_DATA_READABLE_MATCHES_N"].status == GateStatus.BLOCKED_ACCESS
    assert by_id["G4.1_SANDBOX_FEASIBLE"].status == GateStatus.BLOCKED_ACCESS
    assert by_id["G5.1_NUMERIC_OUTCOMES_MATCH"].status == GateStatus.BLOCKED_ACCESS
    assert report.counts["fail"] == 0
    assert report.counts["blocked"] > 0
    assert report.funnel_depth == 0


def test_repaired_execution_distinction_and_any_dependency_mode() -> None:
    registry = GateRegistry.from_yaml()
    paper = _sample_paper()
    raw = _pass_all(registry, paper.primary_profile)
    raw["G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH"] = GateResult(
        gate_id="G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH",
        status=GateStatus.FAIL,
        rationale="Hardcoded /home/author/data path.",
    )
    raw["G4.5_ANALYSIS_EXECUTES_REPAIRED"] = GateResult(
        gate_id="G4.5_ANALYSIS_EXECUTES_REPAIRED",
        status=GateStatus.PASS,
        rationale="Ran after replacing hardcoded path with relative ./data.",
    )

    report = compute_audit_report(paper, registry, raw)
    by_id = {r.gate_id: r for r in report.gate_results}

    assert report.zero_touch_reproducible is False
    assert report.repaired_reproducible is True
    # Because G4.5 passed and G5.1/G5.2 use dependency_mode=any, Stage 5 still scores PASS
    assert by_id["G5.1_NUMERIC_OUTCOMES_MATCH"].status == GateStatus.PASS
    assert by_id["G5.2_FIGURES_REGENERATE"].status == GateStatus.PASS
    assert ProblemDomain.EXECUTION_OUTPUT in report.domain_scores

    md = render_markdown_summary(report)
    assert "# Reproducibility Audit: arxiv:2501.01234" in md
    assert "Domain Breakdown" in md
