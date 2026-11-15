"""Stage 3 data integrity and forensic screening evaluators (G3.7-G3.9)."""

from __future__ import annotations

import re

from researchability.gates.base import EvaluationContext
from researchability.models import EvidenceItem, GateResult, GateStatus
from researchability.profiler.data_profiler import DataFileProfile, profile_data_file
from researchability.profiler.forensics import extract_claimed_sample_sizes, run_forensic_checks
from researchability.profiler.hygiene import scan_code_sentinel_risks

_CODE_EXCLUSION_RE = re.compile(
    r"(\.dropna\s*\(|\.drop_duplicates\s*\(|\.query\s*\(|na\.omit\s*\(|subset\s*\()",
    re.IGNORECASE,
)
_PAPER_EXCLUSION_RE = re.compile(
    r"\b(exclud(?:e|ed|ing)|exclusion[s]?|outlier[s]?|filter(?:ed|ing)?|remov(?:e|ed|ing)|"
    r"missing (?:values|metadata|data)|dropped)\b",
    re.IGNORECASE,
)


def eval_g3_7_data_readable_matches_n(ctx: EvaluationContext) -> GateResult:
    """Profile local data files and verify observed row counts match claimed N."""
    paths = ctx.resolve_relative_files(ctx.paper.artifacts.data_files)
    if not paths:
        return GateResult(
            gate_id="G3.7_DATA_READABLE_MATCHES_N",
            status=GateStatus.N_A,
            rationale="No local data files available to profile.",
        )

    profiles: list[DataFileProfile] = [profile_data_file(p) for p in paths]
    unparseable = [prof for prof in profiles if not prof.parseable]
    if unparseable:
        return GateResult(
            gate_id="G3.7_DATA_READABLE_MATCHES_N",
            status=GateStatus.FAIL,
            rationale="One or more data files failed to parse.",
            evidence=[
                EvidenceItem(
                    source=prof.path,
                    excerpt=prof.parse_error or "parse failure",
                )
                for prof in unparseable
            ],
            flags=[f"Corrupt or unparseable data file: {prof.path}" for prof in unparseable],
        )

    claimed_ns: list[int] = ctx.extracted_claims.get("claimed_n") or extract_claimed_sample_sizes(
        f"{ctx.paper.abstract}\n{ctx.paper.full_text}"
    )
    observed_rows = [prof.row_count for prof in profiles if prof.row_count is not None]

    if claimed_ns and observed_rows:
        # Check if any observed dataset dimension aligns within +/- 10% of any claimed N
        matched = any(
            abs(obs - claim) <= max(2, int(0.1 * claim))
            for obs in observed_rows
            for claim in claimed_ns
        )
        if not matched:
            return GateResult(
                gate_id="G3.7_DATA_READABLE_MATCHES_N",
                status=GateStatus.FAIL,
                rationale=(
                    f"Observed dataset row counts {observed_rows} do not match claimed "
                    f"sample sizes {claimed_ns}."
                ),
                evidence=[
                    EvidenceItem(
                        source=prof.path,
                        excerpt=f"rows={prof.row_count}, cols={prof.column_count}",
                    )
                    for prof in profiles
                    if prof.row_count is not None
                ],
                flags=[
                    f"Sample size mismatch: paper claims {claimed_ns}, data has {observed_rows}"
                ],
            )

    sentinel_map: dict[str, list[str]] = {}
    empty_cols: list[str] = []
    pii_hits: list[str] = []
    all_empty_files: list[str] = []
    for prof in profiles:
        for col, reasons in prof.sentinel_columns.items():
            existing = sentinel_map.setdefault(col, [])
            for r in reasons:
                if r not in existing:
                    existing.append(r)
        for col in prof.empty_columns:
            qualified = f"{prof.path}:{col}"
            if qualified not in empty_cols:
                empty_cols.append(qualified)
        if prof.column_count and len(prof.empty_columns) >= prof.column_count:
            all_empty_files.append(prof.path)
        for hit in prof.pii_findings:
            pii_hits.append(f"{prof.path}: {hit}")

    ctx.extracted_claims["sentinel_columns"] = sentinel_map
    ctx.extracted_claims["data_profiles"] = profiles

    code_texts = ctx.read_text_files(ctx.paper.artifacts.code_files)
    sentinel_code_flags = scan_code_sentinel_risks(code_texts, sentinel_map)
    unguarded_sentinel_filters = [
        f for f in sentinel_code_flags if f.startswith("UNGUARDED_SENTINEL_FILTER")
    ]

    flags: list[str] = []
    for hit in pii_hits[:8]:
        flags.append(f"PII_LEAK_DETECTED: {hit}")
    for col, reasons in list(sentinel_map.items())[:8]:
        flags.append(f"SENTINEL_MISSING_CODES: {col} ({', '.join(reasons)})")
    if empty_cols:
        flags.append(f"EMPTY_DATA_COLUMN: {', '.join(empty_cols[:8])}")
    flags.extend(unguarded_sentinel_filters[:5])

    data_evidence = [
        EvidenceItem(
            source=prof.path,
            excerpt=f"format={prof.format}, rows={prof.row_count}, cols={prof.column_count}",
        )
        for prof in profiles[:5]
    ]

    blocking_reasons: list[str] = []
    if pii_hits:
        blocking_reasons.append(f"potential PII leaked in dataset ({pii_hits[0]})")
    if sentinel_map and unguarded_sentinel_filters:
        blocking_reasons.append(
            "sentinel missing codes contaminated threshold comparison "
            f"({unguarded_sentinel_filters[0]})"
        )
    if all_empty_files:
        blocking_reasons.append(f"all columns 100% missing in {', '.join(all_empty_files[:3])}")

    if blocking_reasons:
        return GateResult(
            gate_id="G3.7_DATA_READABLE_MATCHES_N",
            status=GateStatus.FAIL,
            rationale="Data integrity screening failed: " + "; ".join(blocking_reasons) + ".",
            evidence=data_evidence,
            flags=flags,
        )

    return GateResult(
        gate_id="G3.7_DATA_READABLE_MATCHES_N",
        status=GateStatus.PASS,
        rationale="Data files parsed cleanly and dimensions align with manuscript.",
        evidence=data_evidence,
        flags=flags,
    )


def eval_g3_8_exclusions_documented(ctx: EvaluationContext) -> GateResult:
    """Compare filtering/exclusion operations in prep scripts against paper text."""
    code_texts = ctx.read_text_files(ctx.paper.artifacts.code_files)
    sentinel_map: dict[str, list[str]] | None = ctx.extracted_claims.get("sentinel_columns")
    if sentinel_map is None and ctx.paper.artifacts.data_files:
        computed: dict[str, list[str]] = {}
        for data_path in ctx.resolve_relative_files(ctx.paper.artifacts.data_files):
            prof = profile_data_file(data_path)
            for col, reasons in prof.sentinel_columns.items():
                existing = computed.setdefault(col, [])
                for r in reasons:
                    if r not in existing:
                        existing.append(r)
        sentinel_map = computed
        ctx.extracted_claims["sentinel_columns"] = sentinel_map

    sentinel_flags = scan_code_sentinel_risks(code_texts, sentinel_map)
    unguarded_sentinel = [f for f in sentinel_flags if f.startswith("UNGUARDED_SENTINEL_FILTER")]
    blind_zero = [f for f in sentinel_flags if f.startswith("BLIND_NA_TO_ZERO")]

    code_exclusions: list[EvidenceItem] = []
    for path, content in code_texts.items():
        match = _CODE_EXCLUSION_RE.search(content)
        if match:
            code_exclusions.append(EvidenceItem(source=path, excerpt=match.group(0)))

    if unguarded_sentinel:
        return GateResult(
            gate_id="G3.8_EXCLUSIONS_DOCUMENTED",
            status=GateStatus.FAIL,
            rationale=(
                "Code applies unguarded threshold filtering over sentinel-coded column(s) "
                "without excluding negative/missing codes."
            ),
            evidence=[
                EvidenceItem(source="sentinel_risk", excerpt=item)
                for item in unguarded_sentinel[:3]
            ],
            flags=[*unguarded_sentinel[:5], *blind_zero[:3]],
        )

    if not code_exclusions and not blind_zero:
        return GateResult(
            gate_id="G3.8_EXCLUSIONS_DOCUMENTED",
            status=GateStatus.PASS,
            rationale="No programmatic row exclusions detected in code.",
        )

    paper_has_disclosure = bool(_PAPER_EXCLUSION_RE.search(ctx.manuscript_snippet()))
    if paper_has_disclosure:
        return GateResult(
            gate_id="G3.8_EXCLUSIONS_DOCUMENTED",
            status=GateStatus.PASS,
            rationale="Code filtering aligns with exclusion criteria disclosed in manuscript.",
            evidence=code_exclusions[:3],
            flags=blind_zero[:3],
        )

    fail_evidence = list(code_exclusions[:3])
    fail_flags = [
        f"Undisclosed filtering/exclusion operation in {item.source} ({item.excerpt})"
        for item in code_exclusions[:3]
    ]
    fail_flags.extend(blind_zero[:3])
    return GateResult(
        gate_id="G3.8_EXCLUSIONS_DOCUMENTED",
        status=GateStatus.FAIL,
        rationale=(
            "Code applies row filtering/exclusions or blind NA->0 coercion "
            "not disclosed in manuscript text."
        ),
        evidence=fail_evidence,
        flags=fail_flags,
    )


def eval_g3_9_numeric_consistency(ctx: EvaluationContext) -> GateResult:
    """Run automated forensic numeric consistency checks on manuscript statistics."""
    text = f"{ctx.paper.abstract}\n{ctx.paper.full_text}"
    summary = run_forensic_checks(text)
    if summary.flags:
        return GateResult(
            gate_id="G3.9_NUMERIC_CONSISTENCY",
            status=GateStatus.FAIL,
            rationale=(
                f"Automated forensic check detected {len(summary.flags)} "
                "numeric/statistical consistency issue(s)."
            ),
            evidence=[
                EvidenceItem(
                    source="manuscript",
                    excerpt=res.raw_match,
                    note=f"computed p={res.computed_p:.4f}, decision_error={res.decision_error}",
                )
                for res in summary.statcheck_results
                if not res.consistent
            ]
            or [EvidenceItem(source="manuscript", excerpt=flag) for flag in summary.flags[:3]],
            flags=summary.flags,
        )

    return GateResult(
        gate_id="G3.9_NUMERIC_CONSISTENCY",
        status=GateStatus.PASS,
        rationale=(
            f"Checked {len(summary.statcheck_results)} reported statistic(s) "
            "with no inconsistencies."
        ),
    )


STAGE3_INTEGRITY_EVALUATORS = {
    "G3.7_DATA_READABLE_MATCHES_N": eval_g3_7_data_readable_matches_n,
    "G3.8_EXCLUSIONS_DOCUMENTED": eval_g3_8_exclusions_documented,
    "G3.9_NUMERIC_CONSISTENCY": eval_g3_9_numeric_consistency,
}
