"""Stage 5 evaluators: numeric output verification, figure regeneration, and human dossier."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from researchability.gates.base import EvaluationContext
from researchability.llm.client import ModelTier
from researchability.models import (
    AuditReport,
    EvidenceItem,
    FieldProfile,
    GateResult,
    GateStatus,
    PaperBundle,
)
from researchability.sandbox import SandboxRunResult

_LABELED_METRIC_RE = re.compile(
    r"\b(accuracy|f1(?:[-_ ]?score)?|macro[-_ ]?f1|auroc|roc[-_ ]?auc|auc|precision|recall|"
    r"r2|r\^2|rmse|mae|mse|mape|ate|beta|coef(?:ficient)?|odds[-_ ]?ratio|hazard[-_ ]?ratio|"
    r"correlation|pearson[-_ ]?r|spearman[-_ ]?rho|p[-_ ]?value|loss|bleu|rouge)\b"
    r"\s*(?:of|is|=|:|\approx|~)?\s*([-+]?\d+(?:\.\d+)?)\s*(%?)",
    re.IGNORECASE,
)
_MD_TABLE_ROW_RE = re.compile(
    r"^\|\s*([A-Za-z0-9_ .^()-]{2,40}?)\s*\|\s*([-+]?\d+(?:\.\d+)?)\s*(%?)\s*\|",
    re.MULTILINE,
)
_FIGURE_EXTS = {".png", ".pdf", ".svg", ".jpg", ".jpeg", ".eps", ".tiff"}
_TABLE_EXTS = {".csv", ".tsv", ".tex", ".html", ".json", ".xlsx"}
_DEFAULT_ABS_TOL = 0.005
_DEFAULT_REL_TOL = 0.02


class ReportedNumericClaim(BaseModel):
    """A numeric metric extracted from a paper's text or tables."""

    name: str
    normalized_name: str
    value: float
    is_percent: bool = False
    source: str = "manuscript"


class ReproducedMetric(BaseModel):
    """A numeric metric extracted from sandbox outputs or stdout."""

    name: str
    normalized_name: str
    value: float
    source: str


class NumericComparison(BaseModel):
    """Comparison between a paper-reported value and a sandbox-reproduced value."""

    metric_name: str
    reported_value: float
    reproduced_value: float
    abs_diff: float
    rel_diff: float
    matched: bool
    reported_source: str
    reproduced_source: str


class _LLMNumericList(BaseModel):
    claims: list[ReportedNumericClaim] = Field(default_factory=list)


class _LLMRobustnessChecklist(BaseModel):
    checks: list[str] = Field(default_factory=list)


def _normalize_metric_name(raw: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]", "", raw.lower())
    aliases = {
        "f1score": "f1",
        "macrof1": "macrof1",
        "rocauc": "auc",
        "auroc": "auc",
        "pvalue": "pvalue",
        "coefficient": "coef",
        "beta": "coef",
        "oddsratio": "oddsratio",
        "hazardratio": "hazardratio",
        "acc": "accuracy",
    }
    return aliases.get(cleaned, cleaned)


def _extract_reported_claims(ctx: EvaluationContext) -> list[ReportedNumericClaim]:
    claims: list[ReportedNumericClaim] = []
    seen: set[tuple[str, float]] = set()

    def _add(name: str, val: float, is_percent: bool, source: str) -> None:
        norm = _normalize_metric_name(name)
        if not norm or (norm, val) in seen:
            return
        seen.add((norm, val))
        claims.append(
            ReportedNumericClaim(
                name=name.strip(),
                normalized_name=norm,
                value=val,
                is_percent=is_percent,
                source=source,
            )
        )

    # 1. Structured tables in PaperBundle
    for idx, table in enumerate(ctx.paper.tables):
        raw_rows = table.get("rows")
        rows = raw_rows if isinstance(raw_rows, list) else [table]
        for row in rows:
            if not isinstance(row, dict):
                continue
            metric_label = row.get("metric") or row.get("name")
            metric_val = row.get("value")
            if isinstance(metric_label, str) and isinstance(metric_val, (int, float)):
                _add(metric_label, float(metric_val), False, f"table_{idx}")
                continue
            for key, value in row.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    _add(str(key), float(value), False, f"table_{idx}")

    # 2. Labeled metrics and Markdown table rows across paper text
    text_sources = [
        ("abstract", ctx.paper.abstract),
        *[(f"section:{k}", v) for k, v in ctx.paper.sections.items()],
    ]
    if ctx.paper.full_text and not ctx.paper.sections:
        text_sources.append(("full_text", ctx.paper.full_text[:30_000]))

    for source_name, text in text_sources:
        if not text:
            continue
        for match in _LABELED_METRIC_RE.finditer(text):
            label, num_str, pct = match.group(1), match.group(2), match.group(3)
            try:
                _add(label, float(num_str), pct == "%", source_name)
            except ValueError:
                continue
        for match in _MD_TABLE_ROW_RE.finditer(text):
            label, num_str, pct = match.group(1), match.group(2), match.group(3)
            try:
                _add(label, float(num_str), pct == "%", source_name)
            except ValueError:
                continue

    # 3. Optional Gemini extraction when text yielded no structured claims
    if not claims and ctx.llm is not None:
        snippet = ctx.manuscript_snippet(12_000)
        prompt = (
            "Extract main quantitative reported results "
            "(metric name, numeric value, whether percentage) "
            "from this research paper.\n\n"
            f"{snippet}"
        )
        extracted, _ = ctx.llm.generate_structured(
            paper_id=ctx.paper.paper_id,
            gate_id="G5.1_NUMERIC_OUTCOMES_MATCH",
            prompt=prompt,
            schema_cls=_LLMNumericList,
            tier=ModelTier.FAST,
        )
        for item in extracted.claims:
            _add(item.name, item.value, item.is_percent, item.source or "llm_manuscript")

    return claims


def _flatten_json_metrics(obj: Any, prefix: str = "") -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    if isinstance(obj, dict):
        metric_key = obj.get("metric") or obj.get("name")
        metric_val = obj.get("value")
        if isinstance(metric_key, str) and isinstance(metric_val, (int, float)):
            found.append((metric_key, float(metric_val)))
        for k, v in obj.items():
            key_name = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                found.append((str(k), float(v)))
                if prefix:
                    found.append((key_name, float(v)))
            elif isinstance(v, (dict, list)):
                found.extend(_flatten_json_metrics(v, key_name))
    elif isinstance(obj, list):
        for item in obj[:50]:
            if isinstance(item, dict):
                found.extend(_flatten_json_metrics(item, prefix))
    return found


def _extract_csv_metrics(text: str) -> list[tuple[str, float]]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    delimiter = "\t" if "\t" in lines[0] and "," not in lines[0] else ","
    rows = list(csv.reader(lines, delimiter=delimiter))
    headers = [h.strip() for h in rows[0]]
    found: list[tuple[str, float]] = []

    # Case A: key-value style (e.g., metric,value)
    if len(headers) == 2:
        for row in rows[1:]:
            if len(row) == 2:
                try:
                    found.append((row[0].strip(), float(row[1].strip())))
                except ValueError:
                    continue

    # Case B: single-row summary table or last/mean numeric columns
    data_rows = rows[1:]
    if len(data_rows) == 1:
        for idx, col in enumerate(headers):
            if idx < len(data_rows[0]) and col:
                try:
                    found.append((col, float(data_rows[0][idx].strip())))
                except ValueError:
                    continue
    return found


def _extract_reproduced_metrics(
    run_res: SandboxRunResult,
    analysis_workdir: Path | None,
) -> list[ReproducedMetric]:
    metrics: list[ReproducedMetric] = []
    seen: set[tuple[str, str]] = set()

    def _add(name: str, val: float, source: str) -> None:
        norm = _normalize_metric_name(name)
        if not norm or (norm, source) in seen:
            return
        seen.add((norm, source))
        metrics.append(ReproducedMetric(name=name, normalized_name=norm, value=val, source=source))

    candidate_files = list(dict.fromkeys([*run_res.created_files, *run_res.modified_files]))
    for rel in candidate_files:
        text = run_res.output_previews.get(rel)
        if text is None and analysis_workdir is not None:
            path = analysis_workdir / rel
            if path.is_file() and path.suffix.lower() in {".json", ".csv", ".tsv", ".txt"}:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")[:20_000]
                except OSError:
                    text = None
        if not text:
            continue

        suffix = Path(rel).suffix.lower()
        if suffix == ".json":
            try:
                parsed = json.loads(text)
                for name, val in _flatten_json_metrics(parsed):
                    _add(name, val, f"json:{rel}")
            except json.JSONDecodeError:
                continue
        elif suffix in {".csv", ".tsv"}:
            for name, val in _extract_csv_metrics(text):
                _add(name, val, f"table:{rel}")
        else:
            for match in _LABELED_METRIC_RE.finditer(text):
                try:
                    _add(match.group(1), float(match.group(2)), f"file:{rel}")
                except ValueError:
                    continue

    # Also inspect captured stdout for labeled metrics
    for match in _LABELED_METRIC_RE.finditer(run_res.command_result.stdout):
        try:
            _add(match.group(1), float(match.group(2)), "stdout")
        except ValueError:
            continue

    return metrics


def _values_match(
    reported: float,
    reproduced: float,
    *,
    is_percent: bool,
    abs_tol: float,
    rel_tol: float,
) -> tuple[bool, float, float, float]:
    """Compare values with %/proportion scaling; return (matched, rep, abs_d, rel_d)."""
    candidates = [reported]
    if is_percent or (0.0 <= reproduced <= 1.0 < reported <= 100.0):
        candidates.append(reported / 100.0)
    elif 0.0 <= reported <= 1.0 < reproduced <= 100.0:
        candidates.append(reported * 100.0)

    best_rep = min(candidates, key=lambda c: abs(c - reproduced))
    abs_diff = abs(best_rep - reproduced)
    denom = max(abs(best_rep), 1e-12)
    rel_diff = abs_diff / denom
    matched = abs_diff <= abs_tol or rel_diff <= rel_tol
    return matched, best_rep, abs_diff, rel_diff


def compare_numeric_outcomes(
    reported: list[ReportedNumericClaim],
    reproduced: list[ReproducedMetric],
    *,
    abs_tol: float = _DEFAULT_ABS_TOL,
    rel_tol: float = _DEFAULT_REL_TOL,
) -> list[NumericComparison]:
    """Match paper-reported claims to sandbox metrics and compute deltas."""
    by_norm: dict[str, ReproducedMetric] = {}
    for item in reproduced:
        by_norm.setdefault(item.normalized_name, item)

    comparisons: list[NumericComparison] = []
    for claim in reported:
        target = by_norm.get(claim.normalized_name)
        if target is None:
            # Partial substring match if unambiguous
            matches = [
                item
                for norm_k, item in by_norm.items()
                if claim.normalized_name in norm_k or norm_k in claim.normalized_name
            ]
            if len(matches) == 1:
                target = matches[0]
        if target is None:
            continue

        matched, effective_rep, abs_d, rel_d = _values_match(
            claim.value,
            target.value,
            is_percent=claim.is_percent,
            abs_tol=abs_tol,
            rel_tol=rel_tol,
        )
        comparisons.append(
            NumericComparison(
                metric_name=claim.name,
                reported_value=round(effective_rep, 6),
                reproduced_value=round(target.value, 6),
                abs_diff=round(abs_d, 6),
                rel_diff=round(rel_d, 6),
                matched=matched,
                reported_source=claim.source,
                reproduced_source=target.source,
            )
        )
    return comparisons


def eval_g5_1_numeric_outcomes_match(ctx: EvaluationContext) -> GateResult:
    """Verify executed script outputs match reported numeric outcomes (G5.1)."""
    run_res: SandboxRunResult | None = ctx.extracted_claims.get("analysis_run_effective")
    if run_res is None:
        return GateResult(
            gate_id="G5.1_NUMERIC_OUTCOMES_MATCH",
            status=GateStatus.FAIL,
            rationale="No completed Stage 4 execution output available for numeric comparison.",
        )

    analysis_workdir: Path | None = ctx.extracted_claims.get("analysis_workdir") or ctx.workdir
    reported = _extract_reported_claims(ctx)
    reproduced = _extract_reproduced_metrics(run_res, analysis_workdir)

    abs_tol = float(ctx.extracted_claims.get("numeric_abs_tol", _DEFAULT_ABS_TOL))
    rel_tol = float(ctx.extracted_claims.get("numeric_rel_tol", _DEFAULT_REL_TOL))
    comparisons = compare_numeric_outcomes(reported, reproduced, abs_tol=abs_tol, rel_tol=rel_tol)
    ctx.extracted_claims["numeric_comparisons"] = comparisons

    if not comparisons:
        return GateResult(
            gate_id="G5.1_NUMERIC_OUTCOMES_MATCH",
            status=GateStatus.FAIL,
            rationale=(
                f"Could not match numeric claims between paper ({len(reported)} extracted) "
                f"and sandbox outputs ({len(reproduced)} extracted)."
            ),
            flags=["NO_VERIFIABLE_NUMERIC_METRICS"],
        )

    mismatches = [c for c in comparisons if not c.matched]
    summary_rows = [
        (
            f"{c.metric_name}: reported={c.reported_value} ({c.reported_source}) vs "
            f"reproduced={c.reproduced_value} ({c.reproduced_source}), "
            f"abs_diff={c.abs_diff}"
        )
        for c in comparisons
    ]
    evidence = [EvidenceItem(source="numeric_diff", excerpt="; ".join(summary_rows[:6]))]

    if mismatches:
        mismatch_names = ", ".join(m.metric_name for m in mismatches[:5])
        return GateResult(
            gate_id="G5.1_NUMERIC_OUTCOMES_MATCH",
            status=GateStatus.FAIL,
            rationale=(
                f"{len(mismatches)} of {len(comparisons)} matched metric(s) diverge beyond "
                f"tolerance ({mismatch_names})."
            ),
            evidence=evidence,
            flags=["NUMERIC_OUTCOME_MISMATCH"],
        )

    return GateResult(
        gate_id="G5.1_NUMERIC_OUTCOMES_MATCH",
        status=GateStatus.PASS,
        rationale=f"All {len(comparisons)} matched numeric outcome(s) agree within tolerance.",
        evidence=evidence,
    )


def eval_g5_2_figures_regenerate(ctx: EvaluationContext) -> GateResult:
    """Verify Stage 4 execution produced non-empty tables or figures (G5.2)."""
    run_res: SandboxRunResult | None = ctx.extracted_claims.get("analysis_run_effective")
    if run_res is None:
        return GateResult(
            gate_id="G5.2_FIGURES_REGENERATE",
            status=GateStatus.FAIL,
            rationale="No Stage 4 execution output available to inspect generated artifacts.",
        )

    analysis_workdir: Path | None = ctx.extracted_claims.get("analysis_workdir") or ctx.workdir
    all_touched = list(dict.fromkeys([*run_res.created_files, *run_res.modified_files]))
    valid_artifacts: list[str] = []

    for rel in all_touched:
        suffix = Path(rel).suffix.lower()
        if suffix not in (_FIGURE_EXTS | _TABLE_EXTS):
            continue
        if analysis_workdir is not None and (analysis_workdir / rel).is_file():
            if (analysis_workdir / rel).stat().st_size > 0:
                valid_artifacts.append(rel)
        elif rel in run_res.output_previews and len(run_res.output_previews[rel].strip()) > 0:
            valid_artifacts.append(rel)

    ctx.extracted_claims["regenerated_artifacts"] = valid_artifacts
    if valid_artifacts:
        return GateResult(
            gate_id="G5.2_FIGURES_REGENERATE",
            status=GateStatus.PASS,
            rationale=(
                f"Verified {len(valid_artifacts)} non-empty generated table/figure artifact(s)."
            ),
            evidence=[
                EvidenceItem(
                    source="generated_artifacts",
                    excerpt=", ".join(valid_artifacts[:10]),
                )
            ],
        )

    return GateResult(
        gate_id="G5.2_FIGURES_REGENERATE",
        status=GateStatus.FAIL,
        rationale="Stage 4 execution produced no non-empty figure or table artifacts.",
    )


def _suggest_robustness_checks(
    profile: FieldProfile,
    flags: list[str],
    failed_gates: list[GateResult],
) -> list[str]:
    checks: list[str] = []
    flag_set = set(flags)

    if any("POWER" in f or "SAMPLE" in f for f in flag_set):
        checks.append(
            "Run a post-hoc minimum detectable effect (MDE) and sample-size sensitivity analysis."
        )
    if any("LEAKAGE" in f or "SPLIT" in f for f in flag_set):
        checks.append(
            "Re-evaluate model under strictly held-out grouped/temporal splits "
            "with fit-on-train transforms."
        )
    if any("STATCHECK" in f or "GRIM" in f or "NUMERIC" in f for f in flag_set):
        checks.append(
            "Audit reported test statistics, sample Ns, and rounding against raw subgroup counts."
        )
    if any("REPAIR" in f for f in flag_set):
        checks.append(
            "Verify whether G4.5 path/import edits affect sample alignment or upstream processing."
        )

    if profile == FieldProfile.ML_CS:
        checks.append(
            "Re-run training/evaluation across >= 3 distinct random seeds to measure variance."
        )
    elif profile == FieldProfile.BIOINFORMATICS:
        checks.append(
            "Verify reference genome/annotation version and test multiple-testing FDR sensitivity."
        )
    elif profile in {FieldProfile.PSYCHOLOGY_SOCIAL, FieldProfile.ECONOMICS}:
        checks.append(
            "Perform specification-curve / alternative covariate and cluster-robust SE checks."
        )

    if any(g.gate_id == "G3.5_SENSITIVITY_ANALYSIS" for g in failed_gates):
        checks.append(
            "Add ablation or robustness checks across key hyperparameters and exclusion rules."
        )

    if not checks:
        checks.append(
            "Re-run primary analysis with perturbed random seeds and "
            "alternative subset specifications."
        )
    return list(dict.fromkeys(checks))


def render_human_dossier(
    report: AuditReport,
    *,
    paper: PaperBundle | None = None,
    comparisons: list[NumericComparison] | None = None,
    repair_diffs: list[dict[str, str]] | None = None,
    regenerated_artifacts: list[str] | None = None,
    robustness_checks: list[str] | None = None,
) -> str:
    """Render an I4R-style human review & debate dossier in Markdown."""
    score_pct = (
        f"{report.composite_score * 100:.1f}%" if report.composite_score is not None else "N/A"
    )
    zero_touch = (
        "Yes"
        if report.zero_touch_reproducible is True
        else ("No" if report.zero_touch_reproducible is False else "Not Evaluated")
    )
    repaired = (
        "Yes"
        if report.repaired_reproducible is True
        else ("No" if report.repaired_reproducible is False else "Not Evaluated")
    )

    lines = [
        f"# Human Review & Debate Dossier: {report.paper_id}",
        "",
    ]
    if paper is not None:
        lines.append(f"- **Title:** {paper.title}")
    lines.extend(
        [
            f"- **Source Repository:** `{report.source_repo}`",
            f"- **Field Profile:** `{report.profile.value}`",
            f"- **Funnel Depth:** Stage {report.funnel_depth} / 5",
            f"- **Composite Score:** {score_pct}",
            f"- **Zero-Touch Reproducible (G4.4):** {zero_touch}",
            f"- **Repaired Reproducible (G4.5):** {repaired}",
            (
                f"- **Counts:** {report.counts.get('pass', 0)} PASS, "
                f"{report.counts.get('fail', 0)} FAIL, "
                f"{report.counts.get('blocked', 0)} BLOCKED, "
                f"{report.counts.get('na', 0)} N/A"
            ),
            "",
            "## Gate Summary",
            "",
            "| Gate | Status | Rationale |",
            "| :--- | :---: | :--- |",
        ]
    )
    for res in report.gate_results:
        safe_rat = res.rationale.replace("|", "\\|").replace("\n", " ")[:160]
        lines.append(f"| `{res.gate_id}` | `{res.status.value}` | {safe_rat} |")

    failed_or_blocked = [
        res for res in report.gate_results if res.status == GateStatus.FAIL or res.status.is_blocked
    ]
    if failed_or_blocked:
        lines.extend(["", "## Failed & Blocked Gates", ""])
        for res in failed_or_blocked:
            lines.append(f"- **{res.gate_id}** (`{res.status.value}`): {res.rationale}")
            for ev in res.evidence[:2]:
                lines.append(f"  - Evidence (`{ev.source}`): `{ev.excerpt[:200]}`")

    if report.human_review_flags:
        lines.extend(["", "## Forensic & Methodology Flags", ""])
        for flag in report.human_review_flags:
            lines.append(f"- `{flag}`")

    if repair_diffs:
        lines.extend(["", "## Logged Mechanical Repair Diffs (G4.5)", ""])
        for item in repair_diffs:
            lines.append(f"### `{item.get('file', 'unknown')}` — {item.get('reason', '')}")
            diff_body = item.get("diff", "").strip()
            if diff_body:
                lines.extend(["```diff", diff_body, "```"])

    if comparisons:
        lines.extend(
            [
                "",
                "## Numeric Outcome Comparison (G5.1)",
                "",
                "| Metric | Reported | Reproduced | Abs Diff | Rel Diff | Match |",
                "| :--- | ---: | ---: | ---: | ---: | :---: |",
            ]
        )
        for comp in comparisons:
            match_str = "PASS" if comp.matched else "FAIL"
            lines.append(
                f"| `{comp.metric_name}` | {comp.reported_value} | {comp.reproduced_value} | "
                f"{comp.abs_diff} | {comp.rel_diff:.3%} | **{match_str}** |"
            )

    if regenerated_artifacts:
        lines.extend(["", "## Regenerated Artifacts (G5.2)", ""])
        for art in regenerated_artifacts:
            lines.append(f"- `{art}`")

    checks = robustness_checks or _suggest_robustness_checks(
        report.profile,
        report.human_review_flags,
        [g for g in report.gate_results if g.status == GateStatus.FAIL],
    )
    lines.extend(["", "## Suggested Human Robustness Checks", ""])
    for idx, chk in enumerate(checks, start=1):
        lines.append(f"{idx}. {chk}")

    return "\n".join(lines) + "\n"


def eval_g5_3_human_dossier_ready(ctx: EvaluationContext) -> GateResult:
    """Synthesize I4R-style human review dossier from upstream results (G5.3)."""
    prelim_results: list[GateResult] = list(ctx.extracted_claims.get("preliminary_results", []))
    all_flags = sorted({flag for res in prelim_results for flag in res.flags})
    failed = [res for res in prelim_results if res.status == GateStatus.FAIL]
    checks = _suggest_robustness_checks(ctx.paper.primary_profile, all_flags, failed)

    if ctx.llm is not None:
        flag_summary = ", ".join(all_flags) or "none"
        prompt = (
            "Propose 3 concise, high-leverage robustness checks for human peer debate.\n"
            f"Title: {ctx.paper.title}\n"
            f"Profile: {ctx.paper.primary_profile.value}\n"
            f"Flags: {flag_summary}\n"
        )
        llm_checks, _ = ctx.llm.generate_structured(
            paper_id=ctx.paper.paper_id,
            gate_id="G5.3_HUMAN_DOSSIER_READY",
            prompt=prompt,
            schema_cls=_LLMRobustnessChecklist,
            tier=ModelTier.FAST,
        )
        for item in llm_checks.checks:
            if item.strip() and item.strip() not in checks:
                checks.append(item.strip())

    # Build temporary summary data for dossier preview when full AuditReport isn't passed yet
    comparisons: list[NumericComparison] = ctx.extracted_claims.get("numeric_comparisons", [])
    repair_diffs: list[dict[str, str]] = ctx.extracted_claims.get("repair_diffs", [])
    regenerated: list[str] = ctx.extracted_claims.get("regenerated_artifacts", [])
    ctx.extracted_claims["suggested_robustness_checks"] = checks

    # Construct lightweight report skeleton if not already provided
    prelim_report: AuditReport | None = ctx.extracted_claims.get("preliminary_report")
    if prelim_report is not None:
        ctx.extracted_claims["human_dossier_markdown"] = render_human_dossier(
            prelim_report,
            paper=ctx.paper,
            comparisons=comparisons,
            repair_diffs=repair_diffs,
            regenerated_artifacts=regenerated,
            robustness_checks=checks,
        )

    return GateResult(
        gate_id="G5.3_HUMAN_DOSSIER_READY",
        status=GateStatus.PASS,
        rationale=(
            f"Human review dossier synthesized ({len(checks)} robustness check(s), "
            f"{len(comparisons)} numeric comparison(s), {len(repair_diffs)} repair diff(s))."
        ),
        evidence=[
            EvidenceItem(
                source="human_dossier",
                excerpt="; ".join(checks[:3]),
            )
        ],
    )


STAGE5_EVALUATORS = {
    "G5.1_NUMERIC_OUTCOMES_MATCH": eval_g5_1_numeric_outcomes_match,
    "G5.2_FIGURES_REGENERATE": eval_g5_2_figures_regenerate,
    "G5.3_HUMAN_DOSSIER_READY": eval_g5_3_human_dossier_ready,
}
