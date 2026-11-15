"""Calibration benchmark comparing automated binary gates against human audits."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from researchability.gates import evaluate_stage0_to_3, evaluate_stage0_to_5
from researchability.llm import GeminiClient
from researchability.models import AuditReport, EvaluatorKind, GateStatus, PaperBundle
from researchability.registry import GateRegistry
from researchability.sandbox import DockerSandboxRunner, SandboxConfig


class HumanGateLabel(BaseModel):
    """Ground-truth human audit verdict for a single paper and gate."""

    gate_id: str
    human_status: GateStatus
    notes: str = ""


class CalibrationSample(BaseModel):
    """Gold-standard paper bundle paired with human gate labels and optional precomputed report."""

    paper: PaperBundle
    labels: list[HumanGateLabel] = Field(default_factory=list)
    human_funnel_depth: int | None = Field(default=None, ge=0, le=5)
    workdir: str | None = None
    predicted_report: AuditReport | None = None


class ConfusionCounts(BaseModel):
    """Binary classification confusion matrix treating PASS as positive and FAIL as negative."""

    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    exact_status_matches: int = 0
    total_labels: int = 0


class GateCalibrationMetrics(BaseModel):
    """Reliability metrics for one gate against human labels."""

    gate_id: str
    evaluator_kind: EvaluatorKind = EvaluatorKind.DETERMINISTIC
    counts: ConfusionCounts
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    accuracy: float | None = None
    cohens_kappa: float | None = None
    status_exact_match_rate: float = 0.0


class EvaluatorTierSummary(BaseModel):
    """Reliability rollup grouped by EvaluatorKind (deterministic vs LLM-assisted vs hybrid)."""

    evaluator_kind: EvaluatorKind
    gates_evaluated: int
    total_scorable_pairs: int
    mean_f1: float | None = None
    mean_cohens_kappa: float | None = None


class CalibrationReport(BaseModel):
    """Aggregate benchmark report suitable for methodology validation."""

    sample_count: int
    scorable_pairs: int
    overall_precision: float | None = None
    overall_recall: float | None = None
    overall_f1: float | None = None
    overall_accuracy: float | None = None
    overall_cohens_kappa: float | None = None
    macro_f1: float | None = None
    funnel_depth_mae: float | None = None
    by_evaluator_kind: list[EvaluatorTierSummary] = Field(default_factory=list)
    per_gate: list[GateCalibrationMetrics] = Field(default_factory=list)


def _binary_metrics_from_counts(
    counts: ConfusionCounts,
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    """Compute (precision, recall, f1, accuracy, cohens_kappa) cleanly without division-by-zero."""
    n = counts.tp + counts.fp + counts.fn + counts.tn
    if n == 0:
        return None, None, None, None, None

    precision = counts.tp / (counts.tp + counts.fp) if (counts.tp + counts.fp) > 0 else None
    recall = counts.tp / (counts.tp + counts.fn) if (counts.tp + counts.fn) > 0 else None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1: float | None = 2 * precision * recall / (precision + recall)
    elif precision == 0.0 or recall == 0.0:
        f1 = 0.0
    else:
        f1 = None

    p_o = (counts.tp + counts.tn) / n
    pred_pos = counts.tp + counts.fp
    pred_neg = counts.fn + counts.tn
    true_pos = counts.tp + counts.fn
    true_neg = counts.fp + counts.tn
    p_e = ((pred_pos * true_pos) + (pred_neg * true_neg)) / (n * n)
    if abs(1.0 - p_e) < 1e-12:
        kappa = 1.0 if abs(p_o - 1.0) < 1e-12 else 0.0
    else:
        kappa = (p_o - p_e) / (1.0 - p_e)

    return (
        round(precision, 4) if precision is not None else None,
        round(recall, 4) if recall is not None else None,
        round(f1, 4) if f1 is not None else None,
        round(p_o, 4),
        round(kappa, 4),
    )


def compute_calibration_metrics(
    paired_reports: Sequence[tuple[CalibrationSample, AuditReport]],
    *,
    registry: GateRegistry | None = None,
) -> CalibrationReport:
    """Compute per-gate and overall confusion matrices, F1, and Cohen's kappa."""
    active_registry = registry or GateRegistry.from_yaml()
    gate_defs = {g.id: g for g in active_registry.all_gates()}

    gate_counts: dict[str, ConfusionCounts] = defaultdict(ConfusionCounts)
    depth_errors: list[float] = []

    for sample, report in paired_reports:
        if sample.human_funnel_depth is not None:
            depth_errors.append(abs(report.funnel_depth - sample.human_funnel_depth))

        pred_map = {res.gate_id: res.status for res in report.gate_results}
        for label in sample.labels:
            pred_status = pred_map.get(label.gate_id, GateStatus.N_A)
            bucket = gate_counts[label.gate_id]
            bucket.total_labels += 1
            if pred_status == label.human_status:
                bucket.exact_status_matches += 1

            # Binary confusion considers PASS as positive and non-PASS scorable FAIL as negative;
            # map non-PASS predictions when human rated PASS/FAIL into binary confusion as negative.
            if label.human_status == GateStatus.PASS and pred_status == GateStatus.PASS:
                bucket.tp += 1
            elif label.human_status == GateStatus.FAIL and pred_status == GateStatus.PASS:
                bucket.fp += 1
            elif label.human_status == GateStatus.PASS and pred_status != GateStatus.PASS:
                bucket.fn += 1
            elif label.human_status == GateStatus.FAIL and pred_status != GateStatus.PASS:
                bucket.tn += 1

    per_gate: list[GateCalibrationMetrics] = []
    overall = ConfusionCounts()

    for gate_id in sorted(gate_counts):
        counts = gate_counts[gate_id]
        overall.tp += counts.tp
        overall.fp += counts.fp
        overall.fn += counts.fn
        overall.tn += counts.tn
        overall.exact_status_matches += counts.exact_status_matches
        overall.total_labels += counts.total_labels

        prec, rec, f1, acc, kappa = _binary_metrics_from_counts(counts)
        eval_kind = (
            gate_defs[gate_id].evaluator_kind
            if gate_id in gate_defs
            else EvaluatorKind.DETERMINISTIC
        )
        exact_rate = (
            round(counts.exact_status_matches / counts.total_labels, 4)
            if counts.total_labels > 0
            else 0.0
        )
        per_gate.append(
            GateCalibrationMetrics(
                gate_id=gate_id,
                evaluator_kind=eval_kind,
                counts=counts,
                precision=prec,
                recall=rec,
                f1=f1,
                accuracy=acc,
                cohens_kappa=kappa,
                status_exact_match_rate=exact_rate,
            )
        )

    ov_prec, ov_rec, ov_f1, ov_acc, ov_kappa = _binary_metrics_from_counts(overall)
    f1_vals = [g.f1 for g in per_gate if g.f1 is not None]
    macro_f1 = round(sum(f1_vals) / len(f1_vals), 4) if f1_vals else None
    depth_mae = round(sum(depth_errors) / len(depth_errors), 4) if depth_errors else None

    tier_buckets: dict[EvaluatorKind, list[GateCalibrationMetrics]] = defaultdict(list)
    for g in per_gate:
        tier_buckets[g.evaluator_kind].append(g)

    tier_summaries: list[EvaluatorTierSummary] = []
    for kind, items in sorted(tier_buckets.items(), key=lambda kv: kv[0].value):
        tier_f1s = [x.f1 for x in items if x.f1 is not None]
        tier_kappas = [x.cohens_kappa for x in items if x.cohens_kappa is not None]
        scorable_n = sum(x.counts.tp + x.counts.fp + x.counts.fn + x.counts.tn for x in items)
        tier_summaries.append(
            EvaluatorTierSummary(
                evaluator_kind=kind,
                gates_evaluated=len(items),
                total_scorable_pairs=scorable_n,
                mean_f1=round(sum(tier_f1s) / len(tier_f1s), 4) if tier_f1s else None,
                mean_cohens_kappa=(
                    round(sum(tier_kappas) / len(tier_kappas), 4) if tier_kappas else None
                ),
            )
        )

    return CalibrationReport(
        sample_count=len(paired_reports),
        scorable_pairs=overall.tp + overall.fp + overall.fn + overall.tn,
        overall_precision=ov_prec,
        overall_recall=ov_rec,
        overall_f1=ov_f1,
        overall_accuracy=ov_acc,
        overall_cohens_kappa=ov_kappa,
        macro_f1=macro_f1,
        funnel_depth_mae=depth_mae,
        by_evaluator_kind=tier_summaries,
        per_gate=per_gate,
    )


def evaluate_calibration(
    samples: Sequence[CalibrationSample],
    *,
    registry: GateRegistry | None = None,
    llm: GeminiClient | None = None,
    execute: bool = False,
    sandbox: DockerSandboxRunner | None = None,
) -> CalibrationReport:
    """Run automated gates over labeled papers and benchmark against human ground truth."""
    active_registry = registry or GateRegistry.from_yaml()
    active_sandbox = sandbox or DockerSandboxRunner(SandboxConfig())
    paired: list[tuple[CalibrationSample, AuditReport]] = []

    for sample in samples:
        if sample.predicted_report is not None:
            paired.append((sample, sample.predicted_report))
            continue

        wd = Path(sample.workdir) if sample.workdir else None
        if execute:
            report = evaluate_stage0_to_5(
                paper=sample.paper,
                registry=active_registry,
                workdir=wd,
                llm=llm,
                sandbox=active_sandbox,
            )
        else:
            report = evaluate_stage0_to_3(
                paper=sample.paper,
                registry=active_registry,
                workdir=wd,
                llm=llm,
            )
        paired.append((sample, report))

    return compute_calibration_metrics(paired, registry=active_registry)


def load_calibration_dataset(path: Path) -> list[CalibrationSample]:
    """Load gold-standard CalibrationSamples from a JSON list or JSONL file."""
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    if path.suffix.lower() == ".jsonl":
        return [
            CalibrationSample.model_validate_json(line) for line in raw.splitlines() if line.strip()
        ]
    parsed = json.loads(raw)
    if isinstance(parsed, dict) and isinstance(parsed.get("samples"), list):
        parsed = parsed["samples"]
    return [CalibrationSample.model_validate(item) for item in parsed]


def _fmt_opt(val: float | None, *, pct: bool = True) -> str:
    if val is None:
        return "N/A"
    return f"{val:.1%}" if pct else f"{val:.3f}"


def render_calibration_markdown(report: CalibrationReport) -> str:
    """Render per-gate confusion matrix and Cohen's kappa calibration report."""
    lines = [
        "# Human Ground-Truth Calibration Report",
        "",
        f"- **Labeled Papers:** {report.sample_count}",
        f"- **Scorable Gate-Paper Pairs:** {report.scorable_pairs}",
        f"- **Overall Precision:** {_fmt_opt(report.overall_precision)}",
        f"- **Overall Recall:** {_fmt_opt(report.overall_recall)}",
        f"- **Overall F1:** {_fmt_opt(report.overall_f1)}",
        f"- **Macro F1:** {_fmt_opt(report.macro_f1)}",
        f"- **Overall Accuracy:** {_fmt_opt(report.overall_accuracy)}",
        f"- **Overall Cohen's kappa:** {_fmt_opt(report.overall_cohens_kappa, pct=False)}",
        f"- **Funnel Depth MAE:** {_fmt_opt(report.funnel_depth_mae, pct=False)}",
        "",
        "## Agreement by Evaluator Kind",
        "",
        "| Evaluator Kind | Gates | Pairs | Mean F1 | Mean Kappa |",
        "| :--- | ---: | ---: | ---: | ---: |",
    ]
    for tier in report.by_evaluator_kind:
        lines.append(
            f"| `{tier.evaluator_kind.value}` | {tier.gates_evaluated} | "
            f"{tier.total_scorable_pairs} | {_fmt_opt(tier.mean_f1)} | "
            f"{_fmt_opt(tier.mean_cohens_kappa, pct=False)} |"
        )

    lines.extend(
        [
            "",
            "## Per-Gate Confusion Matrix & Reliability",
            "",
            "| Gate | Kind | TP | FP | FN | TN | Precision | Recall | F1 | Kappa | Exact Match |",
            "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for g in report.per_gate:
        c = g.counts
        lines.append(
            f"| `{g.gate_id}` | `{g.evaluator_kind.value}` | "
            f"{c.tp} | {c.fp} | {c.fn} | {c.tn} | "
            f"{_fmt_opt(g.precision)} | {_fmt_opt(g.recall)} | {_fmt_opt(g.f1)} | "
            f"{_fmt_opt(g.cohens_kappa, pct=False)} | {g.status_exact_match_rate:.1%} |"
        )

    return "\n".join(lines) + "\n"
