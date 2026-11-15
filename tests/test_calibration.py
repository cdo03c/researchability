"""Tests for ground-truth calibration benchmark, confusion matrices, and Cohen's kappa."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from researchability.calibration import (
    CalibrationSample,
    HumanGateLabel,
    compute_calibration_metrics,
    evaluate_calibration,
    load_calibration_dataset,
    render_calibration_markdown,
)
from researchability.cli import app
from researchability.models import (
    ArtifactLink,
    ArtifactManifest,
    ArtifactStatus,
    AuditReport,
    FieldProfile,
    GateResult,
    GateStatus,
    PaperBundle,
)


def _dummy_paper(paper_id: str, *, reproducible: bool) -> PaperBundle:
    if reproducible:
        text = (
            "Empirical evaluation of transformer classifier with sample size n = 50, "
            "random seed 42, 95% CI [0.80, 0.92], p = 0.01, exploratory analysis, limitations."
        )
        return PaperBundle(
            paper_id=paper_id,
            source_repo="arxiv",
            title=f"Reproducible {paper_id}",
            published_year=2024,
            primary_profile=FieldProfile.ML_CS,
            abstract=text,
            full_text=text,
            sections={"methods": text, "results": text},
            artifacts=ArtifactManifest(
                links=[
                    ArtifactLink(
                        url="https://github.com/example/repro",
                        kind="code",
                        host="github",
                        status=ArtifactStatus.RESOLVED,
                    )
                ],
                code_files=["prep.py", "main.py"],
                data_files=["data.csv"],
                manifest_files=["requirements.txt"],
                readme_files=["README.md"],
            ),
        )
    return PaperBundle(
        paper_id=paper_id,
        source_repo="arxiv",
        title=f"Survey Review {paper_id}",
        published_year=2024,
        primary_profile=FieldProfile.ML_CS,
        abstract="Qualitative literature review and conceptual taxonomy without experiments.",
        full_text="Qualitative literature review and conceptual taxonomy without experiments.",
    )


def test_compute_calibration_metrics_exact_kappa_and_confusion() -> None:
    p1 = _dummy_paper("arxiv:1", reproducible=True)
    p2 = _dummy_paper("arxiv:2", reproducible=True)
    p3 = _dummy_paper("arxiv:3", reproducible=False)

    # 3 PASS/FAIL labels for G1.4: (PASS,PASS), (FAIL,FAIL), (FAIL,PASS)->FP=1
    samples_and_reports: list[tuple[CalibrationSample, AuditReport]] = [
        (
            CalibrationSample(
                paper=p1,
                human_funnel_depth=3,
                labels=[
                    HumanGateLabel(gate_id="G1.4_CODE_NON_STUB", human_status=GateStatus.PASS),
                    HumanGateLabel(gate_id="G0.1_EMPIRICAL_CLAIM", human_status=GateStatus.PASS),
                ],
            ),
            AuditReport(
                paper_id="arxiv:1",
                source_repo="arxiv",
                profile=FieldProfile.ML_CS,
                funnel_depth=3,
                gate_results=[
                    GateResult(gate_id="G0.1_EMPIRICAL_CLAIM", status=GateStatus.PASS),
                    GateResult(gate_id="G1.4_CODE_NON_STUB", status=GateStatus.PASS),
                ],
            ),
        ),
        (
            CalibrationSample(
                paper=p2,
                human_funnel_depth=2,
                labels=[
                    HumanGateLabel(gate_id="G1.4_CODE_NON_STUB", human_status=GateStatus.FAIL),
                    HumanGateLabel(gate_id="G0.1_EMPIRICAL_CLAIM", human_status=GateStatus.PASS),
                ],
            ),
            AuditReport(
                paper_id="arxiv:2",
                source_repo="arxiv",
                profile=FieldProfile.ML_CS,
                funnel_depth=1,
                gate_results=[
                    GateResult(gate_id="G0.1_EMPIRICAL_CLAIM", status=GateStatus.PASS),
                    GateResult(gate_id="G1.4_CODE_NON_STUB", status=GateStatus.FAIL),
                ],
            ),
        ),
        (
            CalibrationSample(
                paper=p3,
                human_funnel_depth=0,
                labels=[
                    HumanGateLabel(gate_id="G1.4_CODE_NON_STUB", human_status=GateStatus.FAIL),
                    HumanGateLabel(gate_id="G0.1_EMPIRICAL_CLAIM", human_status=GateStatus.FAIL),
                ],
            ),
            AuditReport(
                paper_id="arxiv:3",
                source_repo="arxiv",
                profile=FieldProfile.ML_CS,
                funnel_depth=0,
                gate_results=[
                    GateResult(gate_id="G0.1_EMPIRICAL_CLAIM", status=GateStatus.FAIL),
                    GateResult(gate_id="G1.4_CODE_NON_STUB", status=GateStatus.PASS),
                ],
            ),
        ),
    ]

    cal = compute_calibration_metrics(samples_and_reports)
    assert cal.sample_count == 3
    assert cal.scorable_pairs == 6
    assert cal.funnel_depth_mae == round(1 / 3, 4)

    g01 = next(g for g in cal.per_gate if g.gate_id == "G0.1_EMPIRICAL_CLAIM")
    assert g01.cohens_kappa == 1.0
    assert g01.f1 == 1.0

    g14 = next(g for g in cal.per_gate if g.gate_id == "G1.4_CODE_NON_STUB")
    assert (g14.counts.tp, g14.counts.fp, g14.counts.fn, g14.counts.tn) == (1, 1, 0, 1)
    assert g14.precision == 0.5
    assert g14.recall == 1.0
    assert cal.by_evaluator_kind

    md = render_calibration_markdown(cal)
    assert "Per-Gate Confusion Matrix & Reliability" in md
    assert "G0.1_EMPIRICAL_CLAIM" in md


def test_live_evaluate_calibration_and_cli_jsonl(tmp_path: Path) -> None:
    s1 = CalibrationSample(
        paper=_dummy_paper("arxiv:2401.1", reproducible=True),
        human_funnel_depth=2,
        labels=[
            HumanGateLabel(gate_id="G0.1_EMPIRICAL_CLAIM", human_status=GateStatus.PASS),
            HumanGateLabel(gate_id="G1.4_CODE_NON_STUB", human_status=GateStatus.PASS),
            HumanGateLabel(gate_id="G2.2_ENV_MANIFEST", human_status=GateStatus.PASS),
        ],
    )
    s2 = CalibrationSample(
        paper=_dummy_paper("arxiv:2401.2", reproducible=False),
        human_funnel_depth=0,
        labels=[
            HumanGateLabel(gate_id="G0.1_EMPIRICAL_CLAIM", human_status=GateStatus.FAIL),
            HumanGateLabel(gate_id="G1.4_CODE_NON_STUB", human_status=GateStatus.N_A),
        ],
    )

    jsonl_path = tmp_path / "benchmark.jsonl"
    jsonl_path.write_text(
        "\n".join([s1.model_dump_json(), s2.model_dump_json()]) + "\n",
        encoding="utf-8",
    )
    loaded = load_calibration_dataset(jsonl_path)
    assert len(loaded) == 2

    direct = evaluate_calibration(loaded)
    assert direct.overall_precision == 1.0
    assert direct.overall_recall == 1.0

    out_json = tmp_path / "cal.json"
    out_md = tmp_path / "cal.md"
    cli = CliRunner()
    res = cli.invoke(
        app,
        [
            "calibrate",
            str(jsonl_path),
            "--output",
            str(out_json),
            "--markdown",
            str(out_md),
        ],
    )
    assert res.exit_code == 0, res.output
    parsed = json.loads(out_json.read_text(encoding="utf-8"))
    assert parsed["overall_f1"] == 1.0
    assert "Cohen's kappa" in out_md.read_text(encoding="utf-8")
