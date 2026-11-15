"""Stage 0 and Stage 1 evaluators: triage, transparency, and incentive bias."""

from __future__ import annotations

import re

from researchability.gates.base import EvaluationContext, run_llm_gate
from researchability.llm.client import ModelTier
from researchability.models import ArtifactStatus, EvidenceItem, GateResult, GateStatus
from researchability.profiler.forensics import extract_claimed_sample_sizes
from researchability.profiler.robustness import (
    PreregRecord,
    diff_preregistration_against_paper,
    fetch_preregistration_summary,
)
from researchability.profiler.spec_checks import audit_data_lineage

_PREP_NAME_RE = re.compile(
    r"(prep|preprocess|clean|etl|ingest|split|build_dataset|make_dataset|curate|filter)",
    re.IGNORECASE,
)
_PREP_CODE_RE = re.compile(
    r"(read_csv|read_parquet|read_table|read\.csv|train_test_split|dropna|drop_duplicates|to_parquet|to_csv)",
    re.IGNORECASE,
)
_EMPIRICAL_RE = re.compile(
    r"\b(accuracy|f1|auc|rmse|mae|loss|benchmark|dataset|cohort|experiment|p\s*[<=>]|n\s*=|table\s+1)\b",
    re.IGNORECASE,
)
_PREREG_RE = re.compile(
    r"(preregister|pre-register|pre-analysis plan|\bpap\b|aspredicted|"
    r"osf\.io/[a-z0-9]+/registr|clinicaltrials\.gov|"
    r"exploratory\s+(?:study|analysis|research|work))",
    re.IGNORECASE,
)
_LIMITATIONS_RE = re.compile(
    r"(limitation|negative result|null result|failure case|failed to|"
    r"no significant difference|does not improve|drawback)",
    re.IGNORECASE,
)
_METHOD_PARAM_RE = re.compile(
    r"(learning rate|batch size|epoch|hyperparameter|split|cross-validation|"
    r"inclusion criteria|exclusion criteria|protocol|optimizer)",
    re.IGNORECASE,
)


def eval_g0_1_empirical_claim(ctx: EvaluationContext) -> GateResult:
    """Evaluate whether the manuscript presents empirical or quantitative claims."""

    def fallback(c: EvaluationContext) -> GateResult:
        text = f"{c.paper.abstract}\n{c.paper.full_text}"
        hits = _EMPIRICAL_RE.findall(text)
        has_artifacts = bool(
            c.paper.tables or c.paper.artifacts.data_files or c.paper.artifacts.code_files
        )
        passed = len(hits) >= 2 or has_artifacts
        excerpt = ", ".join(hits[:4]) if hits else "artifacts present"
        return GateResult(
            gate_id="G0.1_EMPIRICAL_CLAIM",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            confidence=0.8,
            rationale=(
                "Quantitative/empirical indicators found."
                if passed
                else "Insufficient empirical indicators; appears theoretical or survey."
            ),
            evidence=[EvidenceItem(source="manuscript", excerpt=excerpt)] if passed else [],
        )

    return run_llm_gate(
        ctx,
        gate_id="G0.1_EMPIRICAL_CLAIM",
        prompt=(
            "Does this paper present empirical, computational, or quantitative experimental "
            "claims (rather than a pure mathematical theory or qualitative survey)?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        tier=ModelTier.FAST,
        fallback=fallback,
    )


def eval_g1_1_data_link_present(ctx: EvaluationContext) -> GateResult:
    """Evaluate whether data source links or bundled data files are present."""
    data_links = [lnk for lnk in ctx.paper.artifacts.links if lnk.kind == "data"]
    if data_links or ctx.paper.artifacts.data_files:
        sources = [lnk.url for lnk in data_links] + ctx.paper.artifacts.data_files[:3]
        return GateResult(
            gate_id="G1.1_DATA_LINK_PRESENT",
            status=GateStatus.PASS,
            rationale="Data link or bundled dataset files detected.",
            evidence=[EvidenceItem(source="artifacts", excerpt="; ".join(sources[:5]))],
        )

    return run_llm_gate(
        ctx,
        gate_id="G1.1_DATA_LINK_PRESENT",
        prompt=(
            "Does the manuscript explicitly state where raw or evaluation data can be obtained?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        fallback=lambda _: GateResult(
            gate_id="G1.1_DATA_LINK_PRESENT",
            status=GateStatus.FAIL,
            rationale="No dataset link, DOI, or bundled data files detected.",
        ),
    )


def eval_g1_2_data_accessible(ctx: EvaluationContext) -> GateResult:
    """Determine if referenced data artifacts resolve and are accessible."""
    local_data = ctx.resolve_relative_files(ctx.paper.artifacts.data_files)
    if local_data:
        return GateResult(
            gate_id="G1.2_DATA_ACCESSIBLE",
            status=GateStatus.PASS,
            rationale=f"Found {len(local_data)} accessible local data file(s).",
            evidence=[
                EvidenceItem(
                    source="workspace",
                    excerpt=", ".join(str(p.name) for p in local_data[:5]),
                )
            ],
        )

    data_links = [lnk for lnk in ctx.paper.artifacts.links if lnk.kind == "data"]
    if any(lnk.status == ArtifactStatus.BLOCKED_ACCESS for lnk in data_links):
        return GateResult(
            gate_id="G1.2_DATA_ACCESSIBLE",
            status=GateStatus.BLOCKED_ACCESS,
            rationale="Data repository requires restricted/IRB access.",
            flags=["Data access restricted; requires human credentials or IRB approval"],
        )

    if any(lnk.status == ArtifactStatus.BLOCKED_DATA_SIZE for lnk in data_links):
        return GateResult(
            gate_id="G1.2_DATA_ACCESSIBLE",
            status=GateStatus.BLOCKED_DATA_SIZE,
            rationale="Dataset exceeds configured download size budget.",
            flags=["Dataset too large for automated download"],
        )

    if ctx.paper.artifacts.data_files and not any(
        lnk.status == ArtifactStatus.BROKEN_404 for lnk in data_links
    ):
        return GateResult(
            gate_id="G1.2_DATA_ACCESSIBLE",
            status=GateStatus.PASS,
            rationale=f"Found {len(ctx.paper.artifacts.data_files)} accessible data file(s).",
            evidence=[
                EvidenceItem(
                    source="workspace",
                    excerpt=", ".join(ctx.paper.artifacts.data_files[:5]),
                )
            ],
        )

    if not data_links:
        return GateResult(
            gate_id="G1.2_DATA_ACCESSIBLE",
            status=GateStatus.FAIL,
            rationale="No resolvable data links available.",
        )

    if any(lnk.status == ArtifactStatus.RESOLVED for lnk in data_links):
        resolved = [lnk.url for lnk in data_links if lnk.status == ArtifactStatus.RESOLVED]
        return GateResult(
            gate_id="G1.2_DATA_ACCESSIBLE",
            status=GateStatus.PASS,
            rationale="At least one data link resolved cleanly.",
            evidence=[EvidenceItem(source="artifact_resolver", excerpt=resolved[0])],
        )

    return GateResult(
        gate_id="G1.2_DATA_ACCESSIBLE",
        status=GateStatus.FAIL,
        rationale="Data links failed to resolve or returned 404.",
        evidence=[EvidenceItem(source="artifact_resolver", excerpt=data_links[0].url)],
    )


def eval_g1_3_code_link_present(ctx: EvaluationContext) -> GateResult:
    """Check whether source code repository links or bundled code files are present."""
    code_links = [lnk for lnk in ctx.paper.artifacts.links if lnk.kind == "code"]
    if code_links or ctx.paper.artifacts.code_files:
        excerpts = [lnk.url for lnk in code_links] + ctx.paper.artifacts.code_files[:3]
        return GateResult(
            gate_id="G1.3_CODE_LINK_PRESENT",
            status=GateStatus.PASS,
            rationale="Source code link or bundled code files found.",
            evidence=[EvidenceItem(source="artifacts", excerpt="; ".join(excerpts[:5]))],
        )

    return run_llm_gate(
        ctx,
        gate_id="G1.3_CODE_LINK_PRESENT",
        prompt=(
            "Does this paper include a link or archive reference for its source code?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        fallback=lambda _: GateResult(
            gate_id="G1.3_CODE_LINK_PRESENT",
            status=GateStatus.FAIL,
            rationale="No source code repository link or code files detected.",
        ),
    )


def eval_g1_4_code_non_stub(ctx: EvaluationContext) -> GateResult:
    """Verify code repository resolves and contains substantive workflow scripts."""
    if ctx.paper.artifacts.code_files:
        return GateResult(
            gate_id="G1.4_CODE_NON_STUB",
            status=GateStatus.PASS,
            rationale=f"Detected {len(ctx.paper.artifacts.code_files)} code file(s).",
            evidence=[
                EvidenceItem(
                    source="workspace",
                    excerpt=", ".join(ctx.paper.artifacts.code_files[:5]),
                )
            ],
        )

    code_links = [lnk for lnk in ctx.paper.artifacts.links if lnk.kind == "code"]
    if any(lnk.status == ArtifactStatus.RESOLVED for lnk in code_links):
        return GateResult(
            gate_id="G1.4_CODE_NON_STUB",
            status=GateStatus.PASS,
            rationale="Code repository resolved as non-stub.",
        )
    if any(lnk.status == ArtifactStatus.BLOCKED_ACCESS for lnk in code_links):
        return GateResult(
            gate_id="G1.4_CODE_NON_STUB",
            status=GateStatus.BLOCKED_ACCESS,
            rationale="Code repository is private or access-restricted.",
        )
    return GateResult(
        gate_id="G1.4_CODE_NON_STUB",
        status=GateStatus.FAIL,
        rationale="Code repository missing, 404, or README-only stub.",
    )


def eval_g1_5_data_prep_code(ctx: EvaluationContext) -> GateResult:
    """Verify whether data-preparation scripts exist and raw->cleaned lineage is intact."""
    texts = ctx.read_text_files(ctx.paper.artifacts.code_files)
    readme_texts = ctx.read_text_files(ctx.paper.artifacts.readme_files)
    lineage = audit_data_lineage(
        texts,
        ctx.paper.artifacts.data_files,
        readme_texts=readme_texts,
        paper_text=ctx.manuscript_snippet(),
    )
    ctx.extracted_claims["lineage_audit"] = lineage

    prep_named = [p for p in ctx.paper.artifacts.code_files if _PREP_NAME_RE.search(p)]
    if prep_named:
        return GateResult(
            gate_id="G1.5_DATA_PREP_CODE",
            status=GateStatus.PASS,
            rationale="Dedicated data-preparation script(s) identified by filename.",
            evidence=[EvidenceItem(source="code_files", excerpt=", ".join(prep_named[:5]))],
            flags=list(lineage.flags),
        )

    for path, content in texts.items():
        match = _PREP_CODE_RE.search(content)
        if match and not lineage.only_processed_without_prep:
            return GateResult(
                gate_id="G1.5_DATA_PREP_CODE",
                status=GateStatus.PASS,
                rationale="Data ingestion/preparation transforms found in code.",
                evidence=[EvidenceItem(source=path, excerpt=match.group(0))],
                flags=list(lineage.flags),
            )

    if lineage.only_processed_without_prep:
        return GateResult(
            gate_id="G1.5_DATA_PREP_CODE",
            status=GateStatus.FAIL,
            rationale=(
                "Only cleaned/processed datasets detected with no upstream raw-data cleaning "
                "script or restricted raw-data disclosure."
            ),
            flags=list(lineage.flags),
        )

    return GateResult(
        gate_id="G1.5_DATA_PREP_CODE",
        status=GateStatus.FAIL,
        rationale="No data preparation or preprocessing code detected.",
        flags=["Only analysis/eval or no data-prep scripts found", *lineage.flags],
    )


def eval_g1_6_method_completeness(ctx: EvaluationContext) -> GateResult:
    """Assess whether methods specify workflow parameters, splits, or protocols."""

    def fallback(c: EvaluationContext) -> GateResult:
        text = c.manuscript_snippet()
        hits = list(dict.fromkeys(m.lower() for m in _METHOD_PARAM_RE.findall(text)))
        passed = len(hits) >= 2
        return GateResult(
            gate_id="G1.6_METHOD_COMPLETENESS",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            confidence=0.7,
            rationale=(
                "Methods describe experimental parameters/splits."
                if passed
                else "Methods lack explicit split, hyperparameter, or protocol details."
            ),
            evidence=[EvidenceItem(source="manuscript", excerpt=", ".join(hits[:5]))]
            if hits
            else [],
        )

    return run_llm_gate(
        ctx,
        gate_id="G1.6_METHOD_COMPLETENESS",
        prompt=(
            "Does the methods description provide sufficient experimental parameters "
            "(splits, hyperparameters, inclusion/exclusion rules, or protocol steps) "
            "for an independent team to repeat the workflow?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        fallback=fallback,
    )


def _check_prereg_diff_flags(ctx: EvaluationContext, prereg_urls: list[str]) -> list[str]:
    cached = ctx.extracted_claims.get("prereg_record")
    record: PreregRecord | None = cached if isinstance(cached, PreregRecord) else None
    if (
        record is None
        and prereg_urls
        and bool(ctx.extracted_claims.get("check_prereg_registry", False))
    ):
        record = fetch_preregistration_summary(
            prereg_urls[0],
            http_get=ctx.extracted_claims.get("prereg_http_get"),
        )
        if record is not None:
            ctx.extracted_claims["prereg_record"] = record
    if record is None:
        return []
    claimed_ns = ctx.extracted_claims.get("claimed_n") or extract_claimed_sample_sizes(
        ctx.manuscript_snippet()
    )
    return diff_preregistration_against_paper(record, ctx.manuscript_snippet(), claimed_ns)


def eval_g1_7_preregistration_disclosed(ctx: EvaluationContext) -> GateResult:
    """Check whether preregistration/PAP or exploratory status is explicitly disclosed."""
    prereg_links = [lnk for lnk in ctx.paper.artifacts.links if lnk.kind == "prereg"]
    if prereg_links:
        diff_flags = _check_prereg_diff_flags(ctx, [lnk.url for lnk in prereg_links])
        return GateResult(
            gate_id="G1.7_PREREGISTRATION_DISCLOSED",
            status=GateStatus.PASS,
            rationale="Preregistration link detected.",
            evidence=[EvidenceItem(source="artifacts", excerpt=prereg_links[0].url)],
            flags=diff_flags,
        )

    def fallback(c: EvaluationContext) -> GateResult:
        match = _PREREG_RE.search(c.manuscript_snippet())
        if match:
            return GateResult(
                gate_id="G1.7_PREREGISTRATION_DISCLOSED",
                status=GateStatus.PASS,
                confidence=0.75,
                rationale="Preregistration or exploratory status explicitly mentioned.",
                evidence=[EvidenceItem(source="manuscript", excerpt=match.group(0))],
            )
        return GateResult(
            gate_id="G1.7_PREREGISTRATION_DISCLOSED",
            status=GateStatus.FAIL,
            confidence=0.75,
            rationale="No preregistration, PAP, or exploratory disclosure found.",
            flags=["No preregistration or exploratory-study disclosure"],
        )

    return run_llm_gate(
        ctx,
        gate_id="G1.7_PREREGISTRATION_DISCLOSED",
        prompt=(
            "Does the paper link a preregistration / Pre-Analysis Plan (PAP), or explicitly "
            "disclose that the study is exploratory / non-preregistered?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        fallback=fallback,
    )


def eval_g1_8_null_negative_reporting(ctx: EvaluationContext) -> GateResult:
    """Check whether limitations, failure cases, or null/negative results are reported."""

    def fallback(c: EvaluationContext) -> GateResult:
        match = _LIMITATIONS_RE.search(c.manuscript_snippet())
        if match:
            return GateResult(
                gate_id="G1.8_NULL_NEGATIVE_REPORTING",
                status=GateStatus.PASS,
                confidence=0.75,
                rationale="Manuscript discusses limitations or negative/null outcomes.",
                evidence=[EvidenceItem(source="manuscript", excerpt=match.group(0))],
            )
        return GateResult(
            gate_id="G1.8_NULL_NEGATIVE_REPORTING",
            status=GateStatus.FAIL,
            confidence=0.7,
            rationale="No limitations, failure modes, or null comparisons reported.",
            flags=["Reports only positive findings without limitations or failure cases"],
        )

    return run_llm_gate(
        ctx,
        gate_id="G1.8_NULL_NEGATIVE_REPORTING",
        prompt=(
            "Does this paper explicitly discuss limitations, failure cases, or null/negative "
            "comparisons rather than presenting exclusively positive outcomes?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        fallback=fallback,
    )


STAGE0_1_EVALUATORS = {
    "G0.1_EMPIRICAL_CLAIM": eval_g0_1_empirical_claim,
    "G1.1_DATA_LINK_PRESENT": eval_g1_1_data_link_present,
    "G1.2_DATA_ACCESSIBLE": eval_g1_2_data_accessible,
    "G1.3_CODE_LINK_PRESENT": eval_g1_3_code_link_present,
    "G1.4_CODE_NON_STUB": eval_g1_4_code_non_stub,
    "G1.5_DATA_PREP_CODE": eval_g1_5_data_prep_code,
    "G1.6_METHOD_COMPLETENESS": eval_g1_6_method_completeness,
    "G1.7_PREREGISTRATION_DISCLOSED": eval_g1_7_preregistration_disclosed,
    "G1.8_NULL_NEGATIVE_REPORTING": eval_g1_8_null_negative_reporting,
}
