"""Tests for DuckDB/Parquet longitudinal store, resumable 50+ paper batch sweeps, and analytics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
from typer.testing import CliRunner

from researchability.batch import BatchConfig, BatchSweepRunner, BatchTarget
from researchability.cli import app
from researchability.models import (
    ArtifactLink,
    ArtifactManifest,
    ArtifactStatus,
    FieldProfile,
    PaperBundle,
)
from researchability.storage import ResearchabilityStore, render_analytics_markdown


class _StubArxiv:
    def __init__(self, fail_id: str | None = None) -> None:
        self.fail_id = fail_id
        self.calls = 0

    def fetch_paper(self, arxiv_id: str, *, workdir: Path | None = None) -> PaperBundle:
        _ = workdir
        self.calls += 1
        if arxiv_id == self.fail_id:
            raise RuntimeError("Simulated transient arXiv fetch failure")
        idx = int(arxiv_id.rsplit(".", maxsplit=1)[-1])
        year = 2021 + (idx % 4)
        profile = FieldProfile.BIOINFORMATICS if idx % 3 == 0 else FieldProfile.ML_CS
        has_code = idx % 2 == 0
        text = (
            "We evaluate accuracy = 0.90 on n = 100 samples with random seed 42, "
            "95% CI [0.85, 0.95], p = 0.01, exploratory study, limitations noted."
            if has_code
            else "Empirical benchmark reporting p = 0.03 without source repository."
        )
        return PaperBundle(
            paper_id=f"arxiv:{arxiv_id}",
            source_repo="arxiv",
            title=f"arXiv Study {idx}",
            authors=["Author A"],
            published_year=year,
            primary_profile=profile,
            abstract=text,
            full_text=text,
            sections={"methods": text, "results": text},
            artifacts=ArtifactManifest(
                links=(
                    [
                        ArtifactLink(
                            url=f"https://github.com/org/repo{idx}",
                            kind="code",
                            host="github",
                            status=ArtifactStatus.RESOLVED,
                        )
                    ]
                    if has_code
                    else []
                ),
                code_files=["prep.py", "main.py"] if has_code else [],
                data_files=["data.csv"] if has_code else [],
                manifest_files=["requirements.txt"] if has_code else [],
                readme_files=["README.md"] if has_code else [],
            ),
        )

    def close(self) -> None:
        return None


class _StubDataverse:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_dataset(self, persistent_id: str, *, workdir: Path | None = None) -> PaperBundle:
        _ = workdir
        self.calls += 1
        idx = int(persistent_id.rsplit("DVN", maxsplit=1)[-1])
        year = 2022 + (idx % 3)
        return PaperBundle(
            paper_id=f"doi:{persistent_id}",
            source_repo="dataverse",
            title=f"Dataverse Study {idx}",
            authors=["Economist B"],
            published_year=year,
            primary_profile=FieldProfile.ECONOMICS,
            abstract="Replication dataset with pre-analysis plan and placebo check (p = 0.02).",
            full_text="Sample size n = 250 with 95% CI [0.1, 0.4] and robustness checks.",
            sections={"results": "p = 0.02, 95% CI [0.1, 0.4]"},
            artifacts=ArtifactManifest(
                links=[
                    ArtifactLink(
                        url=f"https://dataverse.harvard.edu/dataset.xhtml?persistentId={persistent_id}",
                        kind="data",
                        host="dataverse",
                        status=ArtifactStatus.RESOLVED,
                    )
                ],
                code_files=["clean.R", "main.R"],
                data_files=["panel.csv"],
                manifest_files=["renv.lock"],
                readme_files=["README.md"],
            ),
        )

    def close(self) -> None:
        return None


class _NoOpResolver:
    def resolve_bundle(self, paper: PaperBundle, *, workdir: Path | None = None) -> PaperBundle:
        _ = workdir
        return paper

    def close(self) -> None:
        return None


def test_resumable_batch_sweep_over_55_stratified_papers_exports_partitioned_parquet(
    tmp_path: Path,
) -> None:
    targets: list[BatchTarget] = [
        BatchTarget(paper_id=f"2401.{1000 + i}", source_repo="arxiv") for i in range(35)
    ] + [BatchTarget(paper_id=f"10.7910/DVN{200 + i}", source_repo="dataverse") for i in range(20)]
    assert len(targets) == 55

    db_path = tmp_path / "warehouse" / "researchability.duckdb"
    parquet_dir = tmp_path / "warehouse" / "parquet"
    ws_root = tmp_path / "workspaces"

    cfg = BatchConfig(
        db_path=db_path,
        parquet_dir=parquet_dir,
        workspace_root=ws_root,
        cleanup_workspaces=True,
        resume=True,
    )
    stub_arxiv = _StubArxiv(fail_id="2401.1005")
    stub_dv = _StubDataverse()
    stub_res: Any = _NoOpResolver()

    runner = BatchSweepRunner(
        config=cfg,
        arxiv=stub_arxiv,  # type: ignore[arg-type]
        dataverse=stub_dv,  # type: ignore[arg-type]
        resolver=stub_res,
    )
    first = runner.sweep_targets(targets)

    assert first.total_targets == 55
    assert first.processed == 55
    assert first.skipped_resumed == 0
    assert first.succeeded == 54
    assert first.failed == 1
    assert first.failed_ids == ["2401.1005"]
    # Per-paper isolated workspaces cleaned up after scoring
    assert list(ws_root.iterdir()) == []

    # Second run with same target list (and failure cleared) skips 54 completed and retries 1
    stub_arxiv.fail_id = None
    stub_arxiv.calls = 0
    stub_dv.calls = 0
    second = runner.sweep_targets(targets)
    assert second.skipped_resumed == 54
    assert second.processed == 1
    assert second.succeeded == 1
    assert second.failed == 0
    assert stub_arxiv.calls == 1
    assert stub_dv.calls == 0

    # Verify partitioned Parquet datasets via native DuckDB Parquet reader
    reports_pq = parquet_dir / "audit_reports"
    gates_pq = parquet_dir / "gate_results"
    assert any(reports_pq.rglob("*.parquet"))
    assert any(gates_pq.rglob("*.parquet"))

    conn = duckdb.connect()
    try:
        pq_glob = f"{reports_pq}/**/*.parquet"
        cnt = conn.execute(
            f"SELECT COUNT(*) FROM read_parquet('{pq_glob}', hive_partitioning=true)"
        ).fetchone()
        assert cnt is not None and cnt[0] == 55
    finally:
        conn.close()

    # Analytics summary checks
    store = ResearchabilityStore(db_path)
    summary = store.summary_report()
    assert summary.total_papers == 55
    assert summary.funnel_dropoff[0].stage == 0
    assert summary.funnel_dropoff[0].papers_cleared > 0
    assert len(summary.failure_modes) > 0
    assert len(summary.temporal_trends) >= 3
    md = render_analytics_markdown(summary)
    assert "Funnel Drop-off Across Stages" in md
    assert "Temporal & Cross-Field Trends" in md


def test_cli_batch_bundles_and_analytics_commands(tmp_path: Path) -> None:
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir()
    for idx in range(3):
        p = PaperBundle(
            paper_id=f"arxiv:2402.000{idx}",
            source_repo="arxiv",
            title=f"Bundle Paper {idx}",
            published_year=2023 + (idx % 2),
            primary_profile=FieldProfile.ML_CS,
            abstract="Classification accuracy = 0.85 on n = 100 samples, p = 0.01.",
            full_text="Benchmark evaluation with accuracy = 0.85, p = 0.01.",
        )
        (bundles_dir / f"paper_{idx}.json").write_text(p.model_dump_json(), encoding="utf-8")

    db_path = tmp_path / "batch.duckdb"
    pq_dir = tmp_path / "pq"
    out_summary = tmp_path / "batch_summary.json"
    out_md = tmp_path / "analytics.md"

    cli = CliRunner()
    res = cli.invoke(
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
            str(pq_dir),
            "--output",
            str(out_summary),
            "--markdown",
            str(out_md),
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(out_summary.read_text(encoding="utf-8"))
    assert data["succeeded"] == 3
    assert "Total Audited Papers:** 3" in out_md.read_text(encoding="utf-8")

    analytics_out = tmp_path / "queried_analytics.json"
    res2 = cli.invoke(
        app,
        [
            "analytics",
            "--db",
            str(db_path),
            "--output",
            str(analytics_out),
        ],
    )
    assert res2.exit_code == 0, res2.output
    qdata = json.loads(analytics_out.read_text(encoding="utf-8"))
    assert qdata["total_papers"] == 3
