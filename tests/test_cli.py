"""Tests for the researchability CLI and package version metadata."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import researchability.cli as cli_mod
from researchability import __version__
from researchability.cli import app
from researchability.gates.base import EvaluationContext
from researchability.models import (
    ArtifactManifest,
    AuditReport,
    FieldProfile,
    PaperBundle,
)

runner = CliRunner()


def test_package_version_matches_metadata() -> None:
    assert __version__ == "1.1.0.dev0"
    assert version("researchability") == __version__


def test_cli_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("gates", "ingest", "score", "batch", "report"):
        assert cmd in result.stdout


def test_cli_ingest_help_lists_repositories() -> None:
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0
    assert "arxiv" in result.stdout
    assert "dataverse" in result.stdout


def test_cli_gates_profile_filtering() -> None:
    ml_res = runner.invoke(app, ["gates", "--profile", "ml_cs"])
    assert ml_res.exit_code == 0
    assert "G0.1_EMPIRICAL_CLAIM" in ml_res.stdout
    assert "G2.6_BIO_REFERENCE_PINNED" not in ml_res.stdout

    bio_res = runner.invoke(app, ["gates", "--profile", "bioinformatics"])
    assert bio_res.exit_code == 0
    assert "G2.6_BIO_REFERENCE_PINNED" in bio_res.stdout


def test_cli_score_audit_and_report_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper = PaperBundle(
        paper_id="cli_p1",
        source_repo="test",
        title="Empirical Study",
        abstract="We evaluate N=200 samples with accuracy 0.91 across 3 baselines.",
        primary_profile=FieldProfile.ML_CS,
        artifacts=ArtifactManifest(code_files=["train.py"]),
    )
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(paper.model_dump_json(indent=2), encoding="utf-8")

    # 1. score command
    results_path = tmp_path / "results.json"
    results_path.write_text(
        json.dumps(
            [
                {
                    "gate_id": "G0.1_EMPIRICAL_CLAIM",
                    "stage": 0,
                    "domain": "triage",
                    "status": "PASS",
                }
            ]
        ),
        encoding="utf-8",
    )
    scored_path = tmp_path / "scored.json"
    score_res = runner.invoke(
        app,
        ["--verbose", "score", str(bundle_path), str(results_path), "-o", str(scored_path)],
    )
    assert score_res.exit_code == 0
    assert scored_path.is_file()

    # 2. audit offline -> JSON, Markdown, Dossier, Repframe
    audit_json = tmp_path / "audit.json"
    audit_md = tmp_path / "audit.md"
    dossier_md = tmp_path / "dossier.md"
    repframe_json = tmp_path / "repframe.json"
    audit_res = runner.invoke(
        app,
        [
            "audit",
            str(bundle_path),
            "-o",
            str(audit_json),
            "-m",
            str(audit_md),
            "-d",
            str(dossier_md),
            "--repframe",
            str(repframe_json),
        ],
    )
    assert audit_res.exit_code == 0
    assert audit_json.is_file()
    assert audit_md.is_file()
    assert dossier_md.is_file()
    assert repframe_json.is_file()

    # 3. audit --execute with mocked Stage 4-5 runner
    fake_report = AuditReport.model_validate_json(audit_json.read_text(encoding="utf-8"))
    fake_ctx = EvaluationContext(
        paper=paper,
        extracted_claims={
            "human_dossier_markdown": "# Custom Dossier\n",
            "robustness_spec": {"estimator": "ols"},
        },
    )
    monkeypatch.setattr(
        cli_mod,
        "evaluate_stage0_to_5_with_context",
        lambda **_kw: (fake_report, fake_ctx),
    )
    exec_dossier = tmp_path / "exec_dossier.md"
    exec_res = runner.invoke(
        app,
        [
            "audit",
            str(bundle_path),
            "--execute",
            "--timeout",
            "120",
            "-d",
            str(exec_dossier),
            "--repframe",
            str(tmp_path / "exec_repframe.json"),
        ],
    )
    assert exec_res.exit_code == 0
    assert exec_dossier.read_text(encoding="utf-8") == "# Custom Dossier\n"

    # 4. report command
    rendered_md = tmp_path / "rendered.md"
    rep_res = runner.invoke(app, ["report", str(audit_json), "-o", str(rendered_md)])
    assert rep_res.exit_code == 0
    assert rendered_md.is_file()


def test_cli_batch_analytics_and_calibrate(tmp_path: Path) -> None:
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir()
    paper = PaperBundle(
        paper_id="batch_p1",
        source_repo="arxiv",
        title="Batch Test Paper",
        abstract="We evaluate N=150 subjects with accuracy 0.88 and baseline comparison.",
        primary_profile=FieldProfile.ML_CS,
    )
    (bundles_dir / "batch_p1.json").write_text(paper.model_dump_json(), encoding="utf-8")

    db_path = tmp_path / "db.duckdb"
    parquet_dir = tmp_path / "parquet"
    batch_json = tmp_path / "batch.json"
    analytics_md = tmp_path / "analytics.md"
    batch_res = runner.invoke(
        app,
        [
            "batch",
            "--source",
            "bundles",
            "--bundles-dir",
            str(bundles_dir),
            "--db",
            str(db_path),
            "--parquet-dir",
            str(parquet_dir),
            "-o",
            str(batch_json),
            "-m",
            str(analytics_md),
        ],
    )
    assert batch_res.exit_code == 0
    assert batch_json.is_file()
    assert analytics_md.is_file()

    bad_source = runner.invoke(app, ["batch", "--source", "unknown"])
    assert bad_source.exit_code == 2

    analytics_json = tmp_path / "analytics.json"
    analytics_res = runner.invoke(
        app,
        [
            "analytics",
            "--db",
            str(db_path),
            "--profile",
            "ml_cs",
            "-o",
            str(analytics_json),
            "-m",
            str(tmp_path / "analytics2.md"),
        ],
    )
    assert analytics_res.exit_code == 0
    assert analytics_json.is_file()

    cal_dataset = tmp_path / "ground_truth.jsonl"
    cal_dataset.write_text(
        json.dumps(
            {
                "paper": paper.model_dump(mode="json"),
                "human_labels": {"G0.1_EMPIRICAL_CLAIM": "PASS"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    cal_json = tmp_path / "cal.json"
    cal_md = tmp_path / "cal.md"
    cal_res = runner.invoke(
        app,
        [
            "calibrate",
            str(cal_dataset),
            "--profile",
            "ml_cs",
            "--timeout",
            "90",
            "-o",
            str(cal_json),
            "-m",
            str(cal_md),
        ],
    )
    assert cal_res.exit_code == 0
    assert cal_json.is_file()
    assert cal_md.is_file()


def test_cli_ingest_subcommands_and_error_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_bundle = PaperBundle(paper_id="demo_1", source_repo="test", title="Demo")

    class FakeArxiv:
        def __enter__(self) -> FakeArxiv:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def fetch_paper(self, arxiv_id: str, workdir: Path | None = None) -> PaperBundle:
            if arxiv_id == "bad":
                raise httpx.HTTPError("boom")
            return sample_bundle

    class FakeResolver:
        def __enter__(self) -> FakeResolver:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def resolve_bundle(self, bundle: PaperBundle, workdir: Path | None = None) -> PaperBundle:
            return bundle

    class FakeDataverse:
        def __enter__(self) -> FakeDataverse:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def fetch_dataset(
            self, pid: str, *, workdir: Path | None = None, download_files: bool = True
        ) -> PaperBundle:
            return sample_bundle

    class FakeOsf:
        def __enter__(self) -> FakeOsf:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def fetch_project(
            self,
            node_or_url: str,
            *,
            is_preprint: bool = False,
            profile: FieldProfile = FieldProfile.PSYCHOLOGY_SOCIAL,
        ) -> PaperBundle:
            if node_or_url == "bad":
                raise httpx.HTTPError("osf down")
            return sample_bundle

    class FakeIcpsr:
        def __enter__(self) -> FakeIcpsr:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def fetch_project(self, project_or_doi: str) -> PaperBundle:
            if project_or_doi == "bad":
                raise ValueError("invalid id")
            return sample_bundle

    monkeypatch.setattr(cli_mod, "ArxivConnector", FakeArxiv)
    monkeypatch.setattr(cli_mod, "ArtifactResolver", FakeResolver)
    monkeypatch.setattr(cli_mod, "DataverseConnector", FakeDataverse)
    monkeypatch.setattr(cli_mod, "OsfConnector", FakeOsf)
    monkeypatch.setattr(cli_mod, "IcpsrConnector", FakeIcpsr)

    arxiv_out = tmp_path / "arxiv.json"
    assert (
        runner.invoke(app, ["ingest", "arxiv", "2401.00001", "-o", str(arxiv_out)]).exit_code == 0
    )
    assert arxiv_out.is_file()
    assert runner.invoke(app, ["ingest", "arxiv", "bad"]).exit_code == 1

    dv_out = tmp_path / "dv.json"
    assert (
        runner.invoke(
            app, ["ingest", "dataverse", "doi:10.7910/DVN/EX", "-o", str(dv_out)]
        ).exit_code
        == 0
    )
    assert dv_out.is_file()

    osf_out = tmp_path / "osf.json"
    assert runner.invoke(app, ["ingest", "osf", "abcde", "-o", str(osf_out)]).exit_code == 0
    assert osf_out.is_file()
    assert runner.invoke(app, ["ingest", "osf", "bad"]).exit_code == 1

    icpsr_out = tmp_path / "icpsr.json"
    assert runner.invoke(app, ["ingest", "icpsr", "12345", "-o", str(icpsr_out)]).exit_code == 0
    assert icpsr_out.is_file()
    assert runner.invoke(app, ["ingest", "icpsr", "bad"]).exit_code == 1
