"""Unit tests for Phase 2 P1 pipeline correctness fixes."""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from researchability.batch import BatchConfig, BatchSweepRunner, BatchTarget
from researchability.gates import evaluate_stage0_to_3, evaluate_stage0_to_5_with_context
from researchability.gates.base import EvaluationContext
from researchability.gates.stage4_execution import _inject_python_header_safe
from researchability.gates.stage5_verification import eval_g5_3_human_dossier_ready
from researchability.llm.client import GeminiClient, GeminiConfig, ResponseCache
from researchability.models import (
    ArtifactManifest,
    AuditReport,
    DomainScore,
    FieldProfile,
    GateResult,
    GateStatus,
    PaperBundle,
    ProblemDomain,
)
from researchability.profiler.hygiene import audit_dependencies
from researchability.sandbox import CommandResult, DockerSandboxRunner
from researchability.storage.store import ResearchabilityStore


def test_dataverse_batch_resume_matches_prefixed_paper_id(tmp_path: Path) -> None:
    db_path = tmp_path / "db.duckdb"
    store = ResearchabilityStore(db_path)
    paper = PaperBundle(
        paper_id="dataverse:doi:10.7910/DVN/ABC",
        source_repo="dataverse",
        title="DV Dataset",
        primary_profile=FieldProfile.ECONOMICS,
    )
    report = AuditReport(
        paper_id=paper.paper_id,
        source_repo="dataverse",
        profile=FieldProfile.ECONOMICS,
        funnel_depth=2,
        composite_score=0.5,
        core_composite_score=0.5,
        domain_scores={ProblemDomain.TRIAGE: DomainScore(domain=ProblemDomain.TRIAGE, passed=1)},
        gate_results=[
            GateResult(
                gate_id="G0.1_TEXT_ACCESSIBLE",
                stage=0,
                domain=ProblemDomain.TRIAGE,
                status=GateStatus.PASS,
            )
        ],
    )
    store.save_audit(paper, report)

    class FailingDV:
        def fetch_dataset(self, pid: str, workdir: Path | None = None) -> PaperBundle:
            raise AssertionError(f"Should have resumed and skipped {pid}")

        def close(self) -> None:
            pass

    cfg = BatchConfig(
        db_path=db_path,
        parquet_dir=None,
        workspace_root=tmp_path / "ws",
        resume=True,
    )
    with BatchSweepRunner(cfg, store=store, dataverse=FailingDV()) as runner:  # type: ignore[arg-type]
        summary = runner.sweep_targets(
            [BatchTarget(paper_id="doi:10.7910/DVN/ABC", source_repo="dataverse")]
        )
    assert summary.skipped_resumed == 1
    assert summary.processed == 0


def test_store_profile_filtering_and_rollback_preserves_error(tmp_path: Path) -> None:
    store = ResearchabilityStore(tmp_path / "db.duckdb")
    ml_paper = PaperBundle(
        paper_id="ml1", source_repo="arxiv", title="ML", primary_profile=FieldProfile.ML_CS
    )
    econ_paper = PaperBundle(
        paper_id="ec1",
        source_repo="dataverse",
        title="Econ",
        primary_profile=FieldProfile.ECONOMICS,
    )
    store.save_audit(
        ml_paper,
        AuditReport(
            paper_id="ml1",
            source_repo="arxiv",
            profile=FieldProfile.ML_CS,
            published_year=2023,
            funnel_depth=1,
            composite_score=0.5,
            core_composite_score=0.45,
            gate_results=[
                GateResult(
                    gate_id="G1.1_CODE_ACCESSIBLE",
                    stage=1,
                    domain=ProblemDomain.TRANSPARENCY_REPORTING,
                    status=GateStatus.FAIL,
                )
            ],
        ),
    )
    store.save_audit(
        econ_paper,
        AuditReport(
            paper_id="ec1",
            source_repo="dataverse",
            profile=FieldProfile.ECONOMICS,
            published_year=2024,
            funnel_depth=2,
            composite_score=0.9,
            core_composite_score=0.88,
            gate_results=[
                GateResult(
                    gate_id="G2.1_README_ENTRYPOINT",
                    stage=2,
                    domain=ProblemDomain.EXECUTION_OUTPUT,
                    status=GateStatus.FAIL,
                )
            ],
        ),
    )

    econ_summary = store.summary_report(profile=FieldProfile.ECONOMICS)
    assert econ_summary.total_papers == 1
    assert [m.gate_id for m in econ_summary.failure_modes] == ["G2.1_README_ENTRYPOINT"]
    assert all(t.profile == "economics" for t in econ_summary.temporal_trends)

    with store._connect() as conn:
        row = conn.execute(
            "SELECT core_composite_score FROM audit_reports WHERE paper_id = 'ml1'"
        ).fetchone()
    assert row is not None and row[0] == pytest.approx(0.45)


def test_profile_override_propagates_and_dag_short_circuits(tmp_path: Path) -> None:
    # Paper has primary_profile ML_CS, but override ECONOMICS triggers G3.10 with economics profile
    paper = PaperBundle(
        paper_id="p_prof",
        source_repo="arxiv",
        title="Econometric Check",
        primary_profile=FieldProfile.ML_CS,
        abstract="We run DiD with clustered standard errors and placebo checks.",
        full_text="Difference-in-differences specification with placebo test.",
        sections={"Methods": "DiD regression with placebo check."},
    )
    report = evaluate_stage0_to_3(paper, profile=FieldProfile.ECONOMICS)
    assert report.profile == FieldProfile.ECONOMICS
    g310 = next(
        (g for g in report.gate_results if g.gate_id == "G3.10_ECON_ROBUSTNESS_SPECIFICATION"),
        None,
    )
    assert g310 is not None

    # DAG short-circuiting: when G1.3_CODE_LINK_PRESENT fails, G1.4_CODE_NON_STUB is skipped
    called_g14 = False

    def spy_g14(ctx: EvaluationContext) -> GateResult:
        nonlocal called_g14
        called_g14 = True
        return GateResult(
            gate_id="G1.4_CODE_NON_STUB",
            stage=1,
            domain=ProblemDomain.TRANSPARENCY_REPORTING,
            status=GateStatus.PASS,
        )

    no_code_paper = PaperBundle(
        paper_id="p_nocode",
        source_repo="arxiv",
        title="Paper Without Code",
        primary_profile=FieldProfile.ML_CS,
        abstract="We study reproducibility with n = 50 subjects and p = 0.02.",
        full_text="Full manuscript text describing study methods and results.",
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(
            __import__(
                "researchability.gates.runner", fromlist=["ALL_STAGE0_TO_3_EVALUATORS"]
            ).ALL_STAGE0_TO_3_EVALUATORS,
            "G1.4_CODE_NON_STUB",
            spy_g14,
        )
        short_report = evaluate_stage0_to_3(no_code_paper)
    assert not called_g14
    g14 = next(g for g in short_report.gate_results if g.gate_id == "G1.4_CODE_NON_STUB")
    assert g14.status == GateStatus.N_A


def test_g5_3_synthesizes_dossier_and_checks_g3_5_real_id() -> None:
    paper = PaperBundle(
        paper_id="p_g53",
        source_repo="arxiv",
        title="Stats Paper",
        primary_profile=FieldProfile.BIOINFORMATICS,
        abstract="Multiple testing study with n = 100.",
    )
    ctx = EvaluationContext(
        paper=paper,
        extracted_claims={
            "preliminary_results": [
                GateResult(
                    gate_id="G3.5_MULTIPLE_TESTING_CONTROLLED",
                    stage=3,
                    domain=ProblemDomain.STATISTICAL_RIGOR,
                    status=GateStatus.FAIL,
                )
            ]
        },
    )
    res = eval_g5_3_human_dossier_ready(ctx)
    assert res.status == GateStatus.PASS
    md = ctx.extracted_claims.get("human_dossier_markdown", "")
    assert "ablation or robustness" in md.lower()


def test_python_header_safe_injection_and_zero_touch_isolation(tmp_path: Path) -> None:
    src = '"""Module doc."""\nfrom __future__ import annotations\nprint("hi")\n'
    patched = _inject_python_header_safe(src, "import os\nos.makedirs('out', exist_ok=True)\n")
    # Still valid Python syntax & __future__ stays before non-future imports
    tree = ast.parse(patched)
    assert ast.get_docstring(tree) == "Module doc."

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("Run `python main.py`.\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("from pathlib import Path\n", encoding="utf-8")

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
        assert ws != tmp_path  # G4.4 must execute in isolated sub-workspace
        (ws / "generated.txt").write_text("ok\n", encoding="utf-8")
        return CommandResult(returncode=0, stdout="ran", command=args)

    paper = PaperBundle(
        paper_id="p_iso",
        source_repo="test",
        title="Isolation",
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
    _, ctx = evaluate_stage0_to_5_with_context(
        paper,
        workdir=tmp_path,
        sandbox=DockerSandboxRunner(runner=fake_runner),
    )
    assert not (tmp_path / "generated.txt").exists()
    assert (ctx.extracted_claims["analysis_workdir"] / "generated.txt").exists()


def test_hygiene_conda_and_stdlib_and_llm_cache_hardening(tmp_path: Path) -> None:
    res = audit_dependencies(
        code_texts={
            "main.py": "import asyncio\nimport tomllib\nimport numpy as np\nimport torch\n"
        },
        manifest_texts={
            "Dockerfile": "FROM python:3.11-slim\nWORKDIR /app\n",
            "environment.yml": (
                "name: env\nchannels:\n  - conda-forge\ndependencies:\n"
                "  - python=3.11\n  - conda-forge::numpy>=1.26\n  - pip:\n    - torch==2.3.0\n"
            ),
        },
    )
    assert res.undeclared_packages == []

    # Cache keying separates schema_name & temperature and recovers from corrupted JSON
    class OutA(AuditReport):
        pass

    cache = ResponseCache(tmp_path / "cache.sqlite")
    k1, _ = cache.make_key("p", "g", "v1", "m", "hi", schema_name="SchemaA", temperature=0.0)
    k2, _ = cache.make_key("p", "g", "v1", "m", "hi", schema_name="SchemaB", temperature=0.0)
    k3, _ = cache.make_key("p", "g", "v1", "m", "hi", schema_name="SchemaA", temperature=0.5)
    assert len({k1, k2, k3}) == 3

    calls = 0

    def transport(
        model_id: str,
        prompt: str,
        system_instruction: str,
        schema_cls: type[DomainScore],
        temperature: float,
    ) -> str:
        nonlocal calls
        calls += 1
        return DomainScore(domain=ProblemDomain.TRIAGE, passed=2).model_dump_json()

    cfg = GeminiConfig(api_key="test", cache_path=tmp_path / "llm.sqlite")
    client = GeminiClient(config=cfg, transport=transport)  # type: ignore[arg-type]
    # Pre-seed corrupted cache row
    bad_key, ih = client.cache.make_key(
        "p", "g", "v1", cfg.fast_model, "prompt", "", "DomainScore", 0.0
    )
    client.cache.put(
        cache_key=bad_key,
        paper_id="p",
        gate_id="g",
        prompt_version="v1",
        model_id=cfg.fast_model,
        input_hash=ih,
        response_json="{not valid json",
    )
    out, meta = client.generate_structured(
        paper_id="p",
        gate_id="g",
        prompt="prompt",
        schema_cls=DomainScore,
    )
    assert calls == 1
    assert out.passed == 2
    assert not meta.from_cache
    with sqlite3.connect(cfg.cache_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()
    assert mode is not None and mode[0].lower() == "wal"
