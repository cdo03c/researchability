"""Aggregate binary gate results into funnel depth, domain sub-scores, and audit reports."""

from __future__ import annotations

from collections import defaultdict

from researchability.models import (
    AuditReport,
    DependencyMode,
    DomainScore,
    EvidenceItem,
    FieldProfile,
    GateDefinition,
    GateResult,
    GateStatus,
    PaperBundle,
    ProblemDomain,
)
from researchability.registry import GateRegistry


def _resolve_dependency_override(
    gate: GateDefinition,
    resolved: dict[str, GateResult],
) -> GateResult | None:
    """Propagate upstream FAIL, N/A, or BLOCKED_* states according to dependency_mode."""
    if not gate.depends_on:
        return None

    upstream = [resolved[dep_id] for dep_id in gate.depends_on if dep_id in resolved]
    if len(upstream) != len(gate.depends_on):
        missing = [dep_id for dep_id in gate.depends_on if dep_id not in resolved]
        return GateResult(
            gate_id=gate.id,
            status=GateStatus.N_A,
            rationale=f"Skipped because prerequisite gate(s) inactive: {', '.join(missing)}.",
        )

    if gate.dependency_mode == DependencyMode.ANY:
        if any(res.status == GateStatus.PASS for res in upstream):
            return None
        blocked = [res for res in upstream if res.status.is_blocked]
        all_blocked_or_na = all(
            res.status.is_blocked or res.status == GateStatus.N_A for res in upstream
        )
        if blocked and all_blocked_or_na:
            blocker = blocked[0]
            return GateResult(
                gate_id=gate.id,
                status=blocker.status,
                rationale=f"Blocked by upstream gate {blocker.gate_id} ({blocker.status.value}).",
            )
        failed_ids = [res.gate_id for res in upstream]
        return GateResult(
            gate_id=gate.id,
            status=GateStatus.N_A,
            rationale=f"Skipped because no prerequisite gate passed ({', '.join(failed_ids)}).",
        )

    # DependencyMode.ALL: any blocked prerequisite blocks downstream; any FAIL/N_A skips downstream.
    blocked = [res for res in upstream if res.status.is_blocked]
    if blocked:
        blocker = blocked[0]
        return GateResult(
            gate_id=gate.id,
            status=blocker.status,
            rationale=f"Blocked by upstream gate {blocker.gate_id} ({blocker.status.value}).",
        )

    unmet = [res for res in upstream if res.status != GateStatus.PASS]
    if unmet:
        reasons = ", ".join(f"{res.gate_id}={res.status.value}" for res in unmet)
        return GateResult(
            gate_id=gate.id,
            status=GateStatus.N_A,
            rationale=f"Skipped due to unmet prerequisite(s): {reasons}.",
        )

    return None


def _compute_funnel_depth(
    gates: list[GateDefinition],
    results_by_id: dict[str, GateResult],
) -> int:
    """Compute highest consecutive stage (0-5) cleared with no failing or blocked scorable gates."""
    stage_gates: dict[int, list[GateResult]] = defaultdict(list)
    for gate in gates:
        stage_gates[gate.stage].append(results_by_id[gate.id])

    highest_cleared = 0
    for stage in range(0, 6):
        stage_results = stage_gates.get(stage, [])
        if not stage_results:
            continue
        if all(res.status in {GateStatus.PASS, GateStatus.N_A} for res in stage_results) and any(
            res.status == GateStatus.PASS for res in stage_results
        ):
            highest_cleared = stage
        else:
            break
    return highest_cleared


def compute_audit_report(
    paper: PaperBundle,
    registry: GateRegistry,
    raw_results: dict[str, GateResult],
    profile: FieldProfile | None = None,
) -> AuditReport:
    """Resolve dependencies across active gates and compute an AuditReport."""
    active_profile = profile or paper.primary_profile
    active_gates = registry.gates_for_profile(active_profile)

    resolved: dict[str, GateResult] = {}
    ordered_results: list[GateResult] = []
    human_flags: list[str] = []

    for gate in active_gates:
        override = _resolve_dependency_override(gate, resolved)
        if override is not None:
            result = override
        elif gate.id in raw_results:
            result = raw_results[gate.id]
        else:
            result = GateResult(
                gate_id=gate.id,
                status=GateStatus.N_A,
                rationale="Gate evaluator did not produce a result.",
                evidence=[EvidenceItem(source="aggregator", excerpt="missing result")],
            )
        resolved[gate.id] = result
        ordered_results.append(result)
        for flag in result.flags:
            human_flags.append(f"[{gate.id}] {flag}")

    domain_buckets: dict[ProblemDomain, DomainScore] = {
        domain: DomainScore(domain=domain) for domain in ProblemDomain
    }

    total_passed = 0
    total_failed = 0
    total_blocked = 0
    total_na = 0
    weighted_passed = 0.0
    weighted_total = 0.0

    for gate in active_gates:
        res = resolved[gate.id]
        bucket = domain_buckets[gate.problem_domain]
        if res.status == GateStatus.PASS:
            bucket.passed += 1
            bucket.weighted_passed += gate.weight
            bucket.weighted_total += gate.weight
            total_passed += 1
            weighted_passed += gate.weight
            weighted_total += gate.weight
        elif res.status == GateStatus.FAIL:
            bucket.failed += 1
            bucket.weighted_total += gate.weight
            total_failed += 1
            weighted_total += gate.weight
        elif res.status.is_blocked:
            bucket.blocked += 1
            total_blocked += 1
        else:
            bucket.not_applicable += 1
            total_na += 1

    active_domain_scores: dict[ProblemDomain, DomainScore] = {}
    for domain, bucket in domain_buckets.items():
        scorable = bucket.passed + bucket.failed
        if scorable == 0 and bucket.blocked == 0 and bucket.not_applicable == 0:
            continue
        bucket.pass_rate = (bucket.passed / scorable) if scorable > 0 else None
        bucket.weighted_pass_rate = (
            (bucket.weighted_passed / bucket.weighted_total) if bucket.weighted_total > 0 else None
        )
        active_domain_scores[domain] = bucket

    total_scorable = total_passed + total_failed
    unweighted_pass_rate = (total_passed / total_scorable) if total_scorable > 0 else None
    composite_score = (weighted_passed / weighted_total) if weighted_total > 0 else None

    zero_touch_res = resolved.get("G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH")
    repaired_res = resolved.get("G4.5_ANALYSIS_EXECUTES_REPAIRED")
    zero_touch_reproducible = (
        (zero_touch_res.status == GateStatus.PASS)
        if zero_touch_res and zero_touch_res.status.is_scorable
        else None
    )
    repaired_reproducible = (
        True
        if zero_touch_reproducible is True
        else (
            (repaired_res.status == GateStatus.PASS)
            if repaired_res and repaired_res.status.is_scorable
            else None
        )
    )

    return AuditReport(
        paper_id=paper.paper_id,
        source_repo=paper.source_repo,
        profile=active_profile,
        funnel_depth=_compute_funnel_depth(active_gates, resolved),
        composite_score=composite_score,
        unweighted_pass_rate=unweighted_pass_rate,
        zero_touch_reproducible=zero_touch_reproducible,
        repaired_reproducible=repaired_reproducible,
        gate_results=ordered_results,
        domain_scores=active_domain_scores,
        human_review_flags=human_flags,
        counts={
            "pass": total_passed,
            "fail": total_failed,
            "blocked": total_blocked,
            "na": total_na,
        },
    )


def render_markdown_summary(report: AuditReport) -> str:
    """Render a concise human-readable Markdown summary from an AuditReport."""
    score_pct = (
        f"{report.composite_score * 100:.1f}%" if report.composite_score is not None else "N/A"
    )
    lines = [
        f"# Reproducibility Audit: {report.paper_id}",
        "",
        f"- **Source Repository:** {report.source_repo}",
        f"- **Field Profile:** `{report.profile.value}`",
        f"- **Funnel Depth:** Stage {report.funnel_depth} / 5",
        f"- **Composite Score:** {score_pct}",
        (
            f"- **Counts:** {report.counts.get('pass', 0)} PASS, "
            f"{report.counts.get('fail', 0)} FAIL, "
            f"{report.counts.get('blocked', 0)} BLOCKED, "
            f"{report.counts.get('na', 0)} N/A"
        ),
        "",
        "## Domain Breakdown",
        "",
        "| Domain | Pass | Fail | Blocked | N/A | Pass Rate |",
        "| :--- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for domain, ds in report.domain_scores.items():
        rate = f"{ds.pass_rate * 100:.1f}%" if ds.pass_rate is not None else "N/A"
        lines.append(
            f"| `{domain.value}` | {ds.passed} | {ds.failed} | "
            f"{ds.blocked} | {ds.not_applicable} | {rate} |"
        )

    if report.human_review_flags:
        lines.extend(["", "## Flags for Human Judgment", ""])
        for flag in report.human_review_flags:
            lines.append(f"- {flag}")

    lines.extend(["", "## Gate Results", ""])
    for res in report.gate_results:
        note = f" — {res.rationale}" if res.rationale else ""
        lines.append(f"- **{res.gate_id}**: `{res.status.value}`{note}")

    return "\n".join(lines) + "\n"
