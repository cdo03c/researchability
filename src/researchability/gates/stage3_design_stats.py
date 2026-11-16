"""Stage 3 design & statistical rigor evaluators (G3.1-G3.6)."""

from __future__ import annotations

import re

from researchability.gates.base import EvaluationContext, run_llm_gate
from researchability.llm.client import ModelTier
from researchability.models import EvidenceItem, GateResult, GateStatus
from researchability.profiler.forensics import extract_claimed_sample_sizes
from researchability.profiler.spec_checks import audit_model_specifications

_POWER_RE = re.compile(
    r"(power analysis|statistical power|sample[- ]size calculation|minimum detectable|"
    r"1\s*-\s*beta|\b\d+\s+random seeds|\b\d+-fold cross-validation|\b\d+\s+trials)",
    re.IGNORECASE,
)
_BASELINE_RE = re.compile(
    r"\b(baseline|ablation|control group|placebo|state-of-the-art|"
    r"sota|compared against|outperforms)\b",
    re.IGNORECASE,
)
_UNCERTAINTY_RE = re.compile(
    r"(±|\+/-|95%\s*ci|confidence interval|standard error|\bse\b|\bsd\b|std\.|interquartile|iqr)",
    re.IGNORECASE,
)
_PVAL_RE = re.compile(r"\bp\s*[<=>]\s*0?\.\d+", re.IGNORECASE)
_MTC_RE = re.compile(
    r"(bonferroni|benjamini[- ]hochberg|\bfdr\b|false discovery rate|holm|q-value|adjusted p)",
    re.IGNORECASE,
)
_SPLIT_CALL_RE = re.compile(r"train_test_split\s*\(")
_FIT_BEFORE_SPLIT_RE = re.compile(r"\.(?:fit|fit_transform)\s*\(")


def eval_g3_1_sample_size_stated(ctx: EvaluationContext) -> GateResult:
    """Check whether sample sizes or dataset split counts are explicitly stated."""
    text = f"{ctx.paper.abstract}\n{ctx.paper.full_text}"
    claimed = extract_claimed_sample_sizes(text)
    if claimed:
        ctx.extracted_claims["claimed_n"] = claimed
        return GateResult(
            gate_id="G3.1_SAMPLE_SIZE_STATED",
            status=GateStatus.PASS,
            rationale="Explicit sample size(s) detected in manuscript.",
            evidence=[
                EvidenceItem(
                    source="manuscript",
                    excerpt=", ".join(f"N={n}" for n in claimed[:5]),
                )
            ],
        )

    return run_llm_gate(
        ctx,
        gate_id="G3.1_SAMPLE_SIZE_STATED",
        prompt=(
            "Does the paper explicitly report sample sizes (N, cohort sizes, or train/val/test "
            "split counts)?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        fallback=lambda _: GateResult(
            gate_id="G3.1_SAMPLE_SIZE_STATED",
            status=GateStatus.FAIL,
            rationale="No explicit sample sizes or split counts found.",
            flags=["Missing explicit sample size N"],
        ),
    )


def eval_g3_2_power_or_budget_justified(ctx: EvaluationContext) -> GateResult:
    """Check for statistical power analysis or multi-run/fold budget justification."""

    def fallback(c: EvaluationContext) -> GateResult:
        match = _POWER_RE.search(c.manuscript_snippet())
        if match:
            return GateResult(
                gate_id="G3.2_POWER_OR_BUDGET_JUSTIFIED",
                status=GateStatus.PASS,
                confidence=0.75,
                rationale="Power calculation or repeated-evaluation budget stated.",
                evidence=[EvidenceItem(source="manuscript", excerpt=match.group(0))],
            )
        return GateResult(
            gate_id="G3.2_POWER_OR_BUDGET_JUSTIFIED",
            status=GateStatus.FAIL,
            confidence=0.75,
            rationale="No statistical power analysis or sample/run budget justification found.",
            flags=["No power analysis or multi-run budget justification"],
        )

    return run_llm_gate(
        ctx,
        gate_id="G3.2_POWER_OR_BUDGET_JUSTIFIED",
        prompt=(
            "Does the study justify its sample size (e.g., statistical power analysis) "
            "or evaluation budget (e.g., cross-validation folds / multi-seed runs)?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        tier=ModelTier.REASONING,
        fallback=fallback,
    )


def eval_g3_3_controls_and_baselines(ctx: EvaluationContext) -> GateResult:
    """Check whether comparative baselines, controls, or ablations are included."""

    def fallback(c: EvaluationContext) -> GateResult:
        hits = list(dict.fromkeys(m.lower() for m in _BASELINE_RE.findall(c.manuscript_snippet())))
        passed = len(hits) >= 1
        return GateResult(
            gate_id="G3.3_CONTROLS_AND_BASELINES",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            confidence=0.8,
            rationale=(
                "Comparative baselines, controls, or ablations present."
                if passed
                else "No experimental controls, comparative baselines, or ablations detected."
            ),
            evidence=[EvidenceItem(source="manuscript", excerpt=", ".join(hits[:5]))]
            if hits
            else [],
            flags=[] if passed else ["Missing experimental controls or baselines"],
        )

    return run_llm_gate(
        ctx,
        gate_id="G3.3_CONTROLS_AND_BASELINES",
        prompt=(
            "Does the study evaluate against appropriate experimental controls, "
            "comparative baselines, or ablation studies?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        fallback=fallback,
    )


def eval_g3_4_effect_sizes_uncertainty(ctx: EvaluationContext) -> GateResult:
    """Verify uncertainty metrics (CIs, SEs, multi-seed SD) are reported alongside effects."""

    def fallback(c: EvaluationContext) -> GateResult:
        text = c.manuscript_snippet()
        unc_match = _UNCERTAINTY_RE.search(text)
        if unc_match:
            return GateResult(
                gate_id="G3.4_EFFECT_SIZES_UNCERTAINTY",
                status=GateStatus.PASS,
                confidence=0.8,
                rationale="Uncertainty intervals or variance metrics reported.",
                evidence=[EvidenceItem(source="manuscript", excerpt=unc_match.group(0))],
            )
        has_bare_p = bool(_PVAL_RE.search(text))
        flag = (
            "Bare p-values reported without confidence intervals or variance"
            if has_bare_p
            else "Point estimates reported without variance, SE, or confidence intervals"
        )
        return GateResult(
            gate_id="G3.4_EFFECT_SIZES_UNCERTAINTY",
            status=GateStatus.FAIL,
            confidence=0.75,
            rationale="Missing uncertainty intervals (CI, SE, or multi-seed SD).",
            flags=[flag],
        )

    return run_llm_gate(
        ctx,
        gate_id="G3.4_EFFECT_SIZES_UNCERTAINTY",
        prompt=(
            "Do the reported results include effect sizes and uncertainty metrics "
            "(confidence intervals, standard errors, or variance across seeds) rather "
            "than bare p-values or single point estimates?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        tier=ModelTier.REASONING,
        fallback=fallback,
    )


def eval_g3_5_multiple_testing_controlled(ctx: EvaluationContext) -> GateResult:
    """Check whether multiple comparisons or multi-hypothesis testing is controlled."""

    def fallback(c: EvaluationContext) -> GateResult:
        text = c.manuscript_snippet()
        mtc_match = _MTC_RE.search(text)
        if mtc_match:
            return GateResult(
                gate_id="G3.5_MULTIPLE_TESTING_CONTROLLED",
                status=GateStatus.PASS,
                confidence=0.8,
                rationale="Multiple-testing correction method documented.",
                evidence=[EvidenceItem(source="manuscript", excerpt=mtc_match.group(0))],
            )
        pvals = _PVAL_RE.findall(text)
        if len(pvals) <= 1:
            return GateResult(
                gate_id="G3.5_MULTIPLE_TESTING_CONTROLLED",
                status=GateStatus.PASS,
                confidence=0.65,
                rationale="No multi-test p-value multiplicity detected.",
            )
        return GateResult(
            gate_id="G3.5_MULTIPLE_TESTING_CONTROLLED",
            status=GateStatus.FAIL,
            confidence=0.75,
            rationale="Multiple p-values reported without multiple-testing correction.",
            flags=["Multiple hypothesis tests without Bonferroni/FDR adjustment"],
        )

    return run_llm_gate(
        ctx,
        gate_id="G3.5_MULTIPLE_TESTING_CONTROLLED",
        prompt=(
            "When running multiple hypothesis tests or multi-benchmark comparisons, "
            "does the study apply multiple-testing correction or control family-wise error?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        tier=ModelTier.REASONING,
        fallback=fallback,
    )


def eval_g3_6_leakage_hygiene(ctx: EvaluationContext) -> GateResult:
    """Static audit for train/test data leakage patterns in workflow scripts."""
    code_texts = ctx.read_text_files(ctx.paper.artifacts.code_files)
    suspicious: list[EvidenceItem] = []

    for path, content in code_texts.items():
        split_match = _SPLIT_CALL_RE.search(content)
        if not split_match:
            continue
        before_split = content[: split_match.start()]
        fit_match = _FIT_BEFORE_SPLIT_RE.search(before_split)
        if fit_match:
            suspicious.append(
                EvidenceItem(
                    source=path,
                    excerpt=fit_match.group(0),
                    note="Preprocessor fit/fit_transform called before train_test_split",
                )
            )

    spec_audit = audit_model_specifications(ctx.manuscript_snippet(), code_texts)
    ctx.extracted_claims["spec_audit"] = spec_audit

    if suspicious:
        return GateResult(
            gate_id="G3.6_LEAKAGE_HYGIENE",
            status=GateStatus.FAIL,
            confidence=0.85,
            rationale="Suspected data leakage: fit/fit_transform invoked before train_test_split.",
            evidence=suspicious,
            flags=[
                *[
                    f"Suspected preprocessing before train_test_split in {item.source}"
                    for item in suspicious
                ],
                *spec_audit.flags,
            ],
        )

    explicit_spec_mismatch = [f for f in spec_audit.flags if f.startswith("SPECIFICATION_MISMATCH")]
    if explicit_spec_mismatch:
        return GateResult(
            gate_id="G3.6_LEAKAGE_HYGIENE",
            status=GateStatus.FAIL,
            confidence=0.8,
            rationale="Model specification mismatch between code and manuscript claims.",
            evidence=[
                EvidenceItem(source="spec_audit", excerpt=m) for m in explicit_spec_mismatch[:3]
            ],
            flags=list(spec_audit.flags),
        )

    return GateResult(
        gate_id="G3.6_LEAKAGE_HYGIENE",
        status=GateStatus.PASS,
        confidence=0.75,
        rationale="No static pre-split leakage patterns detected in code.",
        flags=list(spec_audit.flags),
    )


STAGE3_DESIGN_STATS_EVALUATORS = {
    "G3.1_SAMPLE_SIZE_STATED": eval_g3_1_sample_size_stated,
    "G3.2_POWER_OR_BUDGET_JUSTIFIED": eval_g3_2_power_or_budget_justified,
    "G3.3_CONTROLS_AND_BASELINES": eval_g3_3_controls_and_baselines,
    "G3.4_EFFECT_SIZES_UNCERTAINTY": eval_g3_4_effect_sizes_uncertainty,
    "G3.5_MULTIPLE_TESTING_CONTROLLED": eval_g3_5_multiple_testing_controlled,
    "G3.6_LEAKAGE_HYGIENE": eval_g3_6_leakage_hygiene,
}
