"""Unit tests for Phase 3 P2 resilience & signal accuracy improvements."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from researchability.batch import BatchConfig, BatchSweepRunner
from researchability.connectors import ArtifactResolver, IcpsrConnector, OsfConnector
from researchability.connectors.arxiv import normalize_arxiv_id
from researchability.connectors.base import RetryTransport
from researchability.gates.base import EvaluationContext, run_llm_gate
from researchability.gates.stage4_execution import _apply_llm_repair
from researchability.gates.stage5_verification import (
    ReportedNumericClaim,
    ReproducedMetric,
    compare_numeric_outcomes,
)
from researchability.llm.client import GeminiApiError, GeminiClient, GeminiConfig
from researchability.models import (
    ArtifactLink,
    ArtifactManifest,
    ArtifactStatus,
    GateResult,
    GateStatus,
    PaperBundle,
    ProblemDomain,
)
from researchability.profiler.data_profiler import profile_data_file
from researchability.profiler.forensics import (
    check_effect_size_bounds,
    check_table_stars_and_cis,
)
from researchability.sandbox import CommandResult, DockerSandboxRunner


def test_retry_transport_retries_transient_statuses_and_errors() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("reset", request=request)
        if attempts == 2:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": True})

    transport = RetryTransport(
        httpx.MockTransport(handler),
        max_retries=2,
        backoff_seconds=0.0,
    )
    with httpx.Client(transport=transport) as client:
        resp = client.get("https://example.org/api")
    assert resp.status_code == 200
    assert attempts == 3


def test_github_resolver_uses_token_and_distinguishes_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_auth: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_auth
        seen_auth = request.headers.get("Authorization")
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0"},
            text="API rate limit exceeded",
        )

    monkeypatch.setenv("GITHUB_TOKEN", "tok_test_123")
    resolver = ArtifactResolver(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    link = ArtifactLink(url="https://github.com/org/repo", host="github", kind="code")
    resolved, *_ = resolver.resolve_link(link)
    assert seen_auth == "Bearer tok_test_123"
    assert resolved.status == ArtifactStatus.UNRESOLVED
    assert resolved.metadata.get("rate_limited") is True


def test_connector_normalization_psyarxiv_doi_and_icpsr_v2() -> None:
    assert normalize_arxiv_id("https://arxiv.org/pdf/2501.00042v2.pdf") == "2501.00042v2"
    assert normalize_arxiv_id("https://arxiv.org/e-print/2501.00042") == "2501.00042"

    def osf_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"attributes": {"title": "Psych Preprint", "description": ""}}},
        )

    osf = OsfConnector(http_client=httpx.Client(transport=httpx.MockTransport(osf_handler)))
    bundle = osf.fetch_project("https://psyarxiv.com/abc12")
    assert bundle.doi == "10.31234/osf.io/abc12"

    def icpsr_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/version/V1/" in url:
            return httpx.Response(404)
        if "/version/V2/" in url:
            return httpx.Response(200, json={"title": "Econ V2", "files": ["main.do"]})
        return httpx.Response(404)

    icpsr = IcpsrConnector(http_client=httpx.Client(transport=httpx.MockTransport(icpsr_handler)))
    ic_bundle = icpsr.fetch_project("12345")
    assert ic_bundle.title == "Econ V2"
    assert ic_bundle.doi == "10.3886/E12345V2"


def test_llm_errors_degrade_gracefully_in_gate_and_repair(tmp_path: Path) -> None:
    def failing_transport(*args: object) -> str:
        raise GeminiApiError("400 invalid api key")

    cfg = GeminiConfig(api_key="bad", cache_path=tmp_path / "cache.sqlite")
    llm = GeminiClient(config=cfg, transport=failing_transport)  # type: ignore[arg-type]
    paper = PaperBundle(paper_id="p_err", source_repo="arxiv", title="Err")
    ctx = EvaluationContext(paper=paper, workdir=tmp_path, llm=llm)

    res = run_llm_gate(
        ctx,
        gate_id="G1.6_METHOD_COMPLETENESS",
        prompt="check",
        fallback=lambda _c: GateResult(
            gate_id="G1.6_METHOD_COMPLETENESS",
            stage=1,
            domain=ProblemDomain.TRANSPARENCY_REPORTING,
            status=GateStatus.PASS,
            rationale="det fallback",
        ),
    )
    assert res.status == GateStatus.PASS
    assert "LLM_FALLBACK_USED" in res.flags

    res_no_fb = run_llm_gate(ctx, gate_id="G3.9_CUSTOM", prompt="check")
    assert res_no_fb.status == GateStatus.N_A
    assert "LLM_CALL_FAILED" in res_no_fb.flags

    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")

    def raise_api_error(**_kwargs: object) -> object:
        raise GeminiApiError("down")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(llm, "generate_structured", raise_api_error)
        assert _apply_llm_repair(ctx, tmp_path, "main.py", "NameError") is None


def test_dockerfile_additional_manifests(tmp_path: Path) -> None:
    runner = DockerSandboxRunner()
    paper = PaperBundle(paper_id="p_m", source_repo="test", title="Manifests")

    ws_uv = tmp_path / "uv_ws"
    ws_uv.mkdir()
    (ws_uv / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    assert "uv sync --frozen --no-dev" in runner.render_dockerfile(paper, ws_uv)

    ws_poetry = tmp_path / "poetry_ws"
    ws_poetry.mkdir()
    (ws_poetry / "poetry.lock").write_text("", encoding="utf-8")
    assert "poetry install" in runner.render_dockerfile(paper, ws_poetry)

    ws_pipenv = tmp_path / "pipenv_ws"
    ws_pipenv.mkdir()
    (ws_pipenv / "Pipfile").write_text("[packages]\n", encoding="utf-8")
    assert "pipenv install --system" in runner.render_dockerfile(paper, ws_pipenv)

    ws_conda = tmp_path / "conda_ws"
    ws_conda.mkdir()
    (ws_conda / "environment.yml").write_text("name: base\n", encoding="utf-8")
    assert "mamba env update" in runner.render_dockerfile(paper, ws_conda)


def test_profiler_and_forensics_false_positive_reduction_and_multimetric(tmp_path: Path) -> None:
    geo_csv = tmp_path / "geo.csv"
    geo_csv.write_text("site_id,latitude,longitude,temp_c\n1,42.3,-71.1,18.4\n", encoding="utf-8")
    prof = profile_data_file(geo_csv)
    assert prof.pii_findings == []

    big_json = tmp_path / "huge.json"
    big_json.write_text("{}", encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("researchability.profiler.data_profiler._MAX_JSON_PROFILE_BYTES", 1)
        jprof = profile_data_file(big_json)
        assert jprof.parseable is False
        assert "cap" in (jprof.parse_error or "").lower()

    # Math formula p = 2 does not trigger impossible p-value, while p = 1.25 does
    assert check_effect_size_bounds("We use the L^p norm with p = 2 throughout.") == []
    assert len(check_effect_size_bounds("Impossible p = 1.25 reported.")) == 1

    # Prose far away from starred table is not flagged as missing stars
    text = (
        "Table 1:\n"
        "A = 0.50*** (0.10)\n"
        "B = 0.40** (0.15)\n" + "\n" * 10 + "In the year 2020.0 (1.0) the baseline shifted."
    )
    assert not any("Missing significance stars" in f for f in check_table_stars_and_cis(text))

    # Multi-instance metric matching chooses the closest candidate among duplicates
    comps = compare_numeric_outcomes(
        [
            ReportedNumericClaim(
                name="accuracy",
                normalized_name="accuracy",
                value=0.91,
                source="paper",
            )
        ],
        [
            ReproducedMetric(
                name="accuracy",
                normalized_name="accuracy",
                value=0.52,
                source="baseline.json",
            ),
            ReproducedMetric(
                name="accuracy",
                normalized_name="accuracy",
                value=0.912,
                source="best_model.json",
            ),
        ],
    )
    assert len(comps) == 1
    assert comps[0].matched is True
    assert comps[0].reproduced_source == "best_model.json"


def test_batch_runner_persists_dossier_when_configured(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data").mkdir()
    (ws / "data" / "x.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    (ws / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    (ws / "README.md").write_text("Run `python main.py`.\n", encoding="utf-8")
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")

    def fake_runner(
        args: list[str], *, cwd: Path | None = None, timeout: int = 300
    ) -> CommandResult:
        if args[1] in {"build", "rmi"}:
            return CommandResult(returncode=0, stdout="ok", command=args)
        target_ws = next(
            (Path(args[i + 1].split(":")[0]) for i, a in enumerate(args) if a == "-v"),
            cwd,
        )
        assert target_ws is not None
        (target_ws / "metrics.json").write_text('{"accuracy": 0.9}', encoding="utf-8")
        return CommandResult(returncode=0, stdout="accuracy = 0.9", command=args)

    dossier_dir = tmp_path / "dossiers"
    cfg = BatchConfig(
        db_path=tmp_path / "db.duckdb",
        parquet_dir=None,
        dossier_dir=dossier_dir,
        execute=True,
    )
    paper = PaperBundle(
        paper_id="p_dossier",
        source_repo="test",
        title="Dossier Batch",
        abstract="We achieve accuracy = 0.9 with n = 10.",
        full_text="We achieve accuracy = 0.9 with n = 10.",
        sections={"Results": "accuracy = 0.9"},
        artifacts=ArtifactManifest(
            code_files=["main.py"],
            data_files=["data/x.csv"],
            manifest_files=["requirements.txt"],
            readme_files=["README.md"],
        ),
    )
    with BatchSweepRunner(cfg, sandbox=DockerSandboxRunner(runner=fake_runner)) as runner:
        runner.run_bundles([paper], workdirs={paper.paper_id: ws})
    assert (dossier_dir / "p_dossier.md").is_file()
