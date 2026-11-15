"""Unit tests for Phase 4 P3 observability, CLI polish, schema migration, and coverage."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import duckdb
import httpx
import pytest
from typer.testing import CliRunner

from researchability.calibration import CalibrationSample, evaluate_calibration
from researchability.cli import app
from researchability.connectors import ArtifactResolver
from researchability.gates.base import EvaluationContext
from researchability.gates.stage4_execution import _maybe_run_multiseed
from researchability.llm.client import GeminiApiError, GeminiClient, GeminiConfig
from researchability.models import (
    ArtifactLink,
    ArtifactManifest,
    ArtifactStatus,
    FieldProfile,
    PaperBundle,
    ProblemDomain,
)
from researchability.profiler.data_profiler import profile_data_file
from researchability.profiler.forensics import check_stat_consistency
from researchability.sandbox import CommandResult, DockerSandboxRunner
from researchability.storage.store import SCHEMA_VERSION, ResearchabilityStore


def test_store_schema_version_and_idempotent_legacy_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.duckdb"
    # Create a legacy pre-v2 audit_reports table without core_composite_score or schema_version
    with duckdb.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE audit_reports (
                paper_id VARCHAR PRIMARY KEY,
                source_repo VARCHAR NOT NULL,
                profile VARCHAR NOT NULL,
                published_year INTEGER,
                generated_at TIMESTAMP NOT NULL,
                funnel_depth INTEGER NOT NULL,
                composite_score DOUBLE,
                unweighted_pass_rate DOUBLE,
                zero_touch_reproducible BOOLEAN,
                repaired_reproducible BOOLEAN,
                passed_count INTEGER NOT NULL,
                failed_count INTEGER NOT NULL,
                blocked_count INTEGER NOT NULL,
                na_count INTEGER NOT NULL,
                flags_json VARCHAR NOT NULL,
                report_json VARCHAR NOT NULL
            )
            """
        )

    store = ResearchabilityStore(db_path)
    # Second init stays idempotent
    ResearchabilityStore(db_path)
    with store._connect() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info('audit_reports')").fetchall()}
        ver_rows = conn.execute("SELECT version FROM schema_version").fetchall()
    assert "core_composite_score" in cols
    assert [r[0] for r in ver_rows] == [SCHEMA_VERSION]


def test_cli_dataverse_download_warning_clean_errors_and_calibrate_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()

    class MockDV:
        def __enter__(self) -> MockDV:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def fetch_dataset(
            self, pid: str, *, workdir: Path | None = None, download_files: bool = True
        ) -> PaperBundle:
            assert workdir is None
            assert download_files is False
            return PaperBundle(paper_id=f"dataverse:{pid}", source_repo="dataverse", title="T")

    monkeypatch.setattr("researchability.cli.DataverseConnector", MockDV)
    res = runner.invoke(app, ["--verbose", "ingest", "dataverse", "doi:10.7910/DVN/TEST"])
    assert res.exit_code == 0
    assert "without --workdir" in res.output

    # Clean error handling on invalid bundle JSON
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{broken", encoding="utf-8")
    err_res = runner.invoke(app, ["audit", str(bad_json)])
    assert err_res.exit_code == 1
    assert "Audit failed" in err_res.output

    # calibrate --profile & --timeout
    cal_file = tmp_path / "cal.json"
    sample = CalibrationSample(
        paper=PaperBundle(
            paper_id="cal1",
            source_repo="arxiv",
            title="Cal",
            primary_profile=FieldProfile.ML_CS,
            abstract="Regression yields R^2 = 0.74 with placebo check.",
            full_text="Full manuscript with placebo test and DiD.",
        ),
        labels=[],
    )
    cal_file.write_text(json.dumps([sample.model_dump(mode="json")]), encoding="utf-8")
    cal_res = runner.invoke(
        app,
        ["calibrate", str(cal_file), "--profile", "economics", "--timeout", "120"],
    )
    assert cal_res.exit_code == 0
    payload = json.loads(cal_res.stdout)
    assert payload["sample_count"] == 1


def test_calibration_skips_sandbox_init_when_execute_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("DockerSandboxRunner should not be instantiated when execute=False")

    monkeypatch.setattr("researchability.calibration.benchmark.DockerSandboxRunner", boom)
    sample = CalibrationSample(
        paper=PaperBundle(paper_id="c1", source_repo="arxiv", title="Test"),
        labels=[],
    )
    rep = evaluate_calibration([sample], execute=False)
    assert rep.sample_count == 1


def test_llm_default_transport_retry_and_error_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class FakeModels:
        def generate_content(self, **kwargs: object) -> object:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("503 service unavailable")
            return types.SimpleNamespace(text='{"domain": "triage", "passed": 1, "failed": 0}')

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            self.models = FakeModels()

    fake_types = types.SimpleNamespace(
        HttpOptions=lambda **kw: kw,
        GenerateContentConfig=lambda **kw: kw,
    )
    fake_genai = types.SimpleNamespace(Client=FakeClient, types=fake_types)
    fake_google = types.SimpleNamespace(genai=fake_genai)
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

    from researchability.models import DomainScore

    client = GeminiClient(
        GeminiConfig(
            api_key="real_test_key",
            cache_path=tmp_path / "cache.sqlite",
            max_retries=2,
            retry_backoff_sec=0.0,
        )
    )
    out, _ = client.generate_structured(
        paper_id="p_retry",
        gate_id="G1",
        prompt="hi",
        schema_cls=DomainScore,
    )
    assert attempts == 2
    assert out.domain == ProblemDomain.TRIAGE

    # Non-transient error wraps into GeminiApiError immediately
    class FatalModels:
        def generate_content(self, **kwargs: object) -> object:
            raise ValueError("invalid schema")

    client._sdk_client = types.SimpleNamespace(models=FatalModels())
    with pytest.raises(GeminiApiError, match="Gemini API call failed"):
        client.generate_structured(
            paper_id="p_fatal",
            gate_id="G2",
            prompt="hi",
            schema_cls=DomainScore,
        )


def test_resolver_http_head_and_data_profiler_jsonl_and_statcheck_chi2(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(200)

    resolver = ArtifactResolver(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    link = ArtifactLink(url="https://example.org/file.tar.gz", host="other")
    res, *_ = resolver.resolve_link(link)
    assert res.status == ArtifactStatus.RESOLVED

    jsonl_path = tmp_path / "records.jsonl"
    jsonl_path.write_text(
        '{"email": "a@b.com", "val": 1}\n{"ip": "10.0.0.1", "val": 2}\n',
        encoding="utf-8",
    )
    prof = profile_data_file(jsonl_path)
    assert prof.parseable is True
    assert prof.row_count == 2
    assert prof.pii_findings

    stats = check_stat_consistency("chi2(2) = 13.82, p = 0.001 and z = 1.96, p = 0.050.")
    assert len(stats) == 2
    assert all(s.consistent for s in stats)


def test_multiseed_runs_in_isolated_subworkspaces(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("Run `python main.py`.\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")

    seen_workspaces: list[Path] = []

    def fake_runner(
        args: list[str], *, cwd: Path | None = None, timeout: int = 300
    ) -> CommandResult:
        if args[1] in {"build", "rmi"}:
            return CommandResult(returncode=0, stdout="ok", command=args)
        ws = next(
            (Path(args[i + 1].split(":")[0]) for i, a in enumerate(args) if a == "-v"),
            cwd,
        )
        assert ws is not None
        seen_workspaces.append(ws)
        (ws / "metrics.json").write_text('{"accuracy": 0.9}', encoding="utf-8")
        return CommandResult(returncode=0, stdout="accuracy = 0.9", command=args)

    paper = PaperBundle(
        paper_id="p_ms",
        source_repo="test",
        title="Multi-seed",
        abstract="We achieve accuracy = 0.9 across multiple seeds with n = 10.",
        full_text="We achieve accuracy = 0.9 across multiple seeds with n = 10.",
        sections={"Results": "accuracy = 0.9"},
        artifacts=ArtifactManifest(
            code_files=["main.py"],
            data_files=["data/x.csv"],
            manifest_files=["requirements.txt"],
            readme_files=["README.md"],
        ),
    )
    sb = DockerSandboxRunner(runner=fake_runner)
    ctx = EvaluationContext(
        paper=paper,
        workdir=tmp_path,
        extracted_claims={"multiseed_values": [11, 22]},
    )
    _maybe_run_multiseed(ctx, sb, "img:1", ["python", "main.py"], tmp_path, None)
    assert len(seen_workspaces) == 2
    assert len(set(seen_workspaces)) == 2
    assert all(ws != tmp_path for ws in seen_workspaces)
