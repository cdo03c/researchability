"""Cross-field overlay gate evaluators for Biomedicine, Psychology, and Economics."""

from __future__ import annotations

import re

from researchability.gates.base import EvaluationContext, run_llm_gate
from researchability.models import EvidenceItem, GateResult, GateStatus

_RRID_AUTH_RE = re.compile(
    r"\b(RRID:[A-Za-z]+_[A-Za-z0-9-]+|STR\s+profil(?:ing|ed)|short\s+tandem\s+repeat|"
    r"mycoplasma\s*(?:free|negative|tested)|authenticated\s+cell\s+line|"
    r"cat(?:alog)?\s*(?:no\.?|#)\s*[A-Za-z0-9-]+)\b",
    re.IGNORECASE,
)
_PROTOCOL_DETAIL_RE = re.compile(
    r"(protocols\.io|bio-protocol\.org|10\.17504/protocols\.io|"
    r"\b\d+(?:\.\d+)?\s*(?:mM|µM|uM|mg/mL|ug/mL|°C|rpm)\b)",
    re.IGNORECASE,
)
_CHECKLIST_RE = re.compile(
    r"\b(ARRIVE(?:\s+2\.0)?|CONSORT|STROBE|PRISMA|SPIRIT|MIAME)\b",
    re.IGNORECASE,
)
_PAP_DEVIATION_RE = re.compile(
    r"(preregist|pre-analysis\s+plan|aspredicted\.org|osf\.io/[a-z0-9]{5}|"
    r"clinicaltrials\.gov|NCT\d{8}|deviation\s+from\s+(?:the\s+)?prereg|"
    r"confirmatory\s+vs\.?\s*exploratory)",
    re.IGNORECASE,
)
_ECON_ROBUST_RE = re.compile(
    r"\b(robustness\s+check|placebo\s+test|falsification|specification\s+curve|"
    r"cluster(?:ed)?\s+standard\s+error|fixed\s+effects?|event\s+study|"
    r"difference-in-differences|instrumental\s+variable|leave-one-out)\b",
    re.IGNORECASE,
)


def eval_g1_9_arrive_consort_checklist(ctx: EvaluationContext) -> GateResult:
    """Verify biomedical study adheres to ARRIVE, CONSORT, STROBE, or PRISMA standards."""

    def fallback(c: EvaluationContext) -> GateResult:
        hits = list(dict.fromkeys(_CHECKLIST_RE.findall(c.manuscript_snippet())))
        passed = len(hits) >= 1
        return GateResult(
            gate_id="G1.9_ARRIVE_CONSORT_CHECKLIST",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            confidence=0.85,
            rationale=(
                f"Biomedical reporting guideline disclosed ({', '.join(hits[:4])})."
                if passed
                else "No ARRIVE, CONSORT, STROBE, or PRISMA reporting checklist citation found."
            ),
            evidence=[EvidenceItem(source="manuscript", excerpt=", ".join(hits[:4]))]
            if hits
            else [],
        )

    return run_llm_gate(
        ctx,
        gate_id="G1.9_ARRIVE_CONSORT_CHECKLIST",
        prompt=(
            "Does this biomedical paper report adherence to an established reporting checklist "
            "(ARRIVE, CONSORT, STROBE, PRISMA, SPIRIT, or MIAME)?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        fallback=fallback,
    )


def eval_g1_10_psych_pap_adherence(ctx: EvaluationContext) -> GateResult:
    """Verify OSF/AsPredicted preregistration link and confirmatory/deviation clarity."""

    def fallback(c: EvaluationContext) -> GateResult:
        prereg_links = [
            link.url
            for link in c.paper.artifacts.links
            if link.kind == "prereg" or link.host in {"osf", "aspredicted", "clinicaltrials"}
        ]
        if prereg_links:
            return GateResult(
                gate_id="G1.10_PSYCH_PAP_ADHERENCE",
                status=GateStatus.PASS,
                confidence=0.9,
                rationale="Preregistration / OSF repository link verified.",
                evidence=[EvidenceItem(source="artifact_links", excerpt=prereg_links[0])],
            )
        hits = list(dict.fromkeys(_PAP_DEVIATION_RE.findall(c.manuscript_snippet())))
        passed = len(hits) >= 1
        return GateResult(
            gate_id="G1.10_PSYCH_PAP_ADHERENCE",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            confidence=0.8,
            rationale=(
                "Preregistered hypotheses or protocol deviations explicitly documented."
                if passed
                else "No OSF/AsPredicted link or confirmatory vs. exploratory distinction found."
            ),
            evidence=[EvidenceItem(source="manuscript", excerpt=", ".join(hits[:4]))]
            if hits
            else [],
        )

    return run_llm_gate(
        ctx,
        gate_id="G1.10_PSYCH_PAP_ADHERENCE",
        prompt=(
            "Does this psychology/behavioral paper link an OSF/AsPredicted preregistration "
            "or pre-analysis plan and separate confirmatory from exploratory analyses?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        fallback=fallback,
    )


def eval_g2_7_wetlab_materials_rrid(ctx: EvaluationContext) -> GateResult:
    """Verify biological cell line authentication (STR/mycoplasma) and reagent RRIDs."""

    def fallback(c: EvaluationContext) -> GateResult:
        hits = list(dict.fromkeys(_RRID_AUTH_RE.findall(c.manuscript_snippet())))
        passed = len(hits) >= 1
        return GateResult(
            gate_id="G2.7_WETLAB_MATERIALS_RRID",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            confidence=0.85,
            rationale=(
                "Reagent RRIDs / cell-line authentication checks documented."
                if passed
                else "No RRIDs, catalog identifiers, STR profiling, or mycoplasma checks found."
            ),
            evidence=[EvidenceItem(source="manuscript", excerpt=", ".join(hits[:5]))]
            if hits
            else [],
            flags=[] if passed else ["MISSING_WETLAB_AUTHENTICATION_OR_RRID"],
        )

    return run_llm_gate(
        ctx,
        gate_id="G2.7_WETLAB_MATERIALS_RRID",
        prompt=(
            "Does this wet-lab study cite antibody/reagent RRIDs or catalog numbers and "
            "report cell line authentication (STR profiling) or mycoplasma checks?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        fallback=fallback,
    )


def eval_g2_8_protocol_repository_link(ctx: EvaluationContext) -> GateResult:
    """Verify wet-lab procedures link protocols.io or provide step-level reagent parameters."""

    def fallback(c: EvaluationContext) -> GateResult:
        proto_links = [
            link.url
            for link in c.paper.artifacts.links
            if link.kind == "protocol" or link.host == "protocols_io"
        ]
        if proto_links:
            return GateResult(
                gate_id="G2.8_PROTOCOL_REPOSITORY_LINK",
                status=GateStatus.PASS,
                confidence=0.95,
                rationale="External protocol repository link detected.",
                evidence=[EvidenceItem(source="artifact_links", excerpt=proto_links[0])],
            )
        hits = list(dict.fromkeys(_PROTOCOL_DETAIL_RE.findall(c.manuscript_snippet())))
        passed = len(hits) >= 2
        return GateResult(
            gate_id="G2.8_PROTOCOL_REPOSITORY_LINK",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            confidence=0.75,
            rationale=(
                "Detailed quantitative reagent/protocol parameters disclosed in methods."
                if passed
                else "No protocols.io link or detailed quantitative wet-lab protocol steps found."
            ),
            evidence=[EvidenceItem(source="manuscript", excerpt=", ".join(hits[:5]))]
            if hits
            else [],
        )

    return run_llm_gate(
        ctx,
        gate_id="G2.8_PROTOCOL_REPOSITORY_LINK",
        prompt=(
            "Does this study link to protocols.io/bio-protocol or include reproducible "
            "step-by-step reagent concentrations, temperatures, and timings?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        fallback=fallback,
    )


def eval_g2_9_stata_r_package_valid(ctx: EvaluationContext) -> GateResult:
    """Verify Stata/R replication package structure for economics studies."""
    code_files = ctx.paper.artifacts.code_files
    econ_scripts = [
        f
        for f in code_files
        if f.lower().endswith((".do", ".ado", ".r", ".rmd", ".py", ".sas", ".sps"))
    ]
    archive_links = [
        link.url
        for link in ctx.paper.artifacts.links
        if link.host in {"icpsr", "dataverse", "zenodo", "osf"}
    ]
    has_readme = bool(ctx.paper.artifacts.readme_files)
    master_named = any(
        any(tok in f.lower() for tok in ("master", "00_", "main", "run_all")) for f in econ_scripts
    )

    if econ_scripts and (has_readme or master_named or archive_links):
        return GateResult(
            gate_id="G2.9_STATA_R_PACKAGE_VALID",
            status=GateStatus.PASS,
            rationale=(
                f"Economics replication package validated ({len(econ_scripts)} Stata/R/analysis "
                "script(s) with master entrypoint, README, or ICPSR/Dataverse link)."
            ),
            evidence=[
                EvidenceItem(
                    source="replication_package",
                    excerpt=f"scripts={econ_scripts[:5]}, archives={archive_links[:3]}",
                )
            ],
        )

    return GateResult(
        gate_id="G2.9_STATA_R_PACKAGE_VALID",
        status=GateStatus.FAIL,
        rationale=(
            "Incomplete Stata/R replication package (missing master script, README table guide, "
            "or ICPSR/Dataverse archive metadata)."
        ),
    )


def eval_g3_10_econ_robustness_specification(ctx: EvaluationContext) -> GateResult:
    """Verify robustness/placebo specifications and record repframe-ready metadata."""

    def fallback(c: EvaluationContext) -> GateResult:
        hits = list(
            dict.fromkeys(h.lower() for h in _ECON_ROBUST_RE.findall(c.manuscript_snippet()))
        )
        c.extracted_claims["robustness_spec"] = {
            "profile": c.paper.primary_profile.value,
            "detected_methods": hits,
            "scripts": [
                f for f in c.paper.artifacts.code_files if f.lower().endswith((".do", ".r", ".py"))
            ],
            "data_files": c.paper.artifacts.data_files,
        }
        passed = len(hits) >= 1
        return GateResult(
            gate_id="G3.10_ECON_ROBUSTNESS_SPECIFICATION",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            confidence=0.85,
            rationale=(
                f"Alternative/placebo robustness checks documented ({', '.join(hits[:4])})."
                if passed
                else "No placebo, specification sensitivity, or alternative estimator checks found."
            ),
            evidence=[EvidenceItem(source="manuscript", excerpt=", ".join(hits[:5]))]
            if hits
            else [],
        )

    return run_llm_gate(
        ctx,
        gate_id="G3.10_ECON_ROBUSTNESS_SPECIFICATION",
        prompt=(
            "Does this study evaluate alternative specifications, placebo/falsification tests, "
            "or clustering/fixed-effects robustness checks?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        fallback=fallback,
    )


STAGE_CROSSFIELD_EVALUATORS = {
    "G1.9_ARRIVE_CONSORT_CHECKLIST": eval_g1_9_arrive_consort_checklist,
    "G1.10_PSYCH_PAP_ADHERENCE": eval_g1_10_psych_pap_adherence,
    "G2.7_WETLAB_MATERIALS_RRID": eval_g2_7_wetlab_materials_rrid,
    "G2.8_PROTOCOL_REPOSITORY_LINK": eval_g2_8_protocol_repository_link,
    "G2.9_STATA_R_PACKAGE_VALID": eval_g2_9_stata_r_package_valid,
    "G3.10_ECON_ROBUSTNESS_SPECIFICATION": eval_g3_10_econ_robustness_specification,
}
