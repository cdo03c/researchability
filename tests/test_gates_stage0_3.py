"""End-to-end tests for Stage 0-3 gate evaluators (deterministic & Gemini-backed)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from researchability.cli import app
from researchability.gates import evaluate_stage0_to_3
from researchability.llm.client import GeminiClient, GeminiConfig
from researchability.models import (
    ArtifactLink,
    ArtifactManifest,
    ArtifactStatus,
    FieldProfile,
    GateStatus,
    PaperBundle,
)


def _create_well_packaged_workspace(root: Path) -> PaperBundle:
    (root / "README.md").write_text(
        "# Study\nRun `python prep.py && python train.py` to reproduce Table 1.\n",
        encoding="utf-8",
    )
    (root / "requirements.txt").write_text("numpy==2.1.0\nscikit-learn==1.5.2\n", encoding="utf-8")
    (root / "prep.py").write_text(
        "import pandas as pd\ndf = pd.read_csv('data.csv').dropna()\n",
        encoding="utf-8",
    )
    (root / "train.py").write_text(
        "import random\nrandom.seed(42)\n# model training after split\n",
        encoding="utf-8",
    )
    rows = "\n".join(f"{i},{i * 0.5}" for i in range(100))
    (root / "data.csv").write_text(f"id,val\n{rows}\n", encoding="utf-8")

    return PaperBundle(
        paper_id="arxiv:2502.00001",
        source_repo="arxiv",
        title="Well-Packaged Bioinformatics Benchmark",
        primary_profile=FieldProfile.BIOINFORMATICS,
        abstract=(
            "We analyze N = 100 samples aligned to GRCh38 using 5 random seeds on an NVIDIA A100 "
            "GPU. Accuracy is 91.2% ± 0.4% (95% CI [90.4, 92.0]), outperforming baseline models "
            "with Benjamini-Hochberg FDR adjustment (t(98) = 2.63, p = 0.010). "
            "Samples with missing metadata were excluded during preprocessing. "
            "This study is exploratory and not preregistered; limitations include cohort size."
        ),
        full_text=(
            "Methods: learning rate 1e-4, batch size 32, 5-fold cross-validation split, "
            "exclusion criteria documented. Reference build GRCh38 and GSE123456."
        ),
        artifacts=ArtifactManifest(
            links=[
                ArtifactLink(
                    url="https://github.com/org/bio-bench",
                    host="github",
                    kind="code",
                    status=ArtifactStatus.RESOLVED,
                    local_path=str(root),
                ),
                ArtifactLink(
                    url="https://zenodo.org/records/99999",
                    host="zenodo",
                    kind="data",
                    status=ArtifactStatus.RESOLVED,
                    local_path=str(root),
                ),
            ],
            code_files=["prep.py", "train.py"],
            data_files=["data.csv"],
            manifest_files=["requirements.txt"],
            readme_files=["README.md"],
        ),
    )


def test_stage0_to_3_evaluators_pass_on_well_packaged_paper(tmp_path: Path) -> None:
    paper = _create_well_packaged_workspace(tmp_path)
    report = evaluate_stage0_to_3(paper, workdir=tmp_path)
    by_id = {r.gate_id: r for r in report.gate_results}

    assert report.funnel_depth == 3
    assert by_id["G0.1_EMPIRICAL_CLAIM"].status == GateStatus.PASS
    assert by_id["G1.5_DATA_PREP_CODE"].status == GateStatus.PASS
    assert by_id["G1.7_PREREGISTRATION_DISCLOSED"].status == GateStatus.PASS
    assert by_id["G1.8_NULL_NEGATIVE_REPORTING"].status == GateStatus.PASS
    assert by_id["G2.1_README_ENTRYPOINT"].status == GateStatus.PASS
    assert by_id["G2.3_VERSIONS_PINNED"].status == GateStatus.PASS
    assert by_id["G2.4_STOCHASTIC_CONTROLS"].status == GateStatus.PASS
    assert by_id["G2.6_BIO_REFERENCE_PINNED"].status == GateStatus.PASS
    assert by_id["G3.1_SAMPLE_SIZE_STATED"].status == GateStatus.PASS
    assert by_id["G3.4_EFFECT_SIZES_UNCERTAINTY"].status == GateStatus.PASS
    assert by_id["G3.6_LEAKAGE_HYGIENE"].status == GateStatus.PASS
    assert by_id["G3.7_DATA_READABLE_MATCHES_N"].status == GateStatus.PASS
    assert by_id["G3.8_EXCLUSIONS_DOCUMENTED"].status == GateStatus.PASS
    assert by_id["G3.9_NUMERIC_CONSISTENCY"].status == GateStatus.PASS


def test_stage3_catches_leakage_wrong_n_undocumented_exclusions_and_p_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "prep.py").write_text(
        "df = load().dropna()\nscaler.fit_transform(X)\nX_tr, X_te = train_test_split(X)\n",
        encoding="utf-8",
    )
    (tmp_path / "data.csv").write_text("id,x\n1,2\n2,3\n", encoding="utf-8")

    paper = PaperBundle(
        paper_id="arxiv:2502.99999",
        source_repo="arxiv",
        title="Flawed Study",
        primary_profile=FieldProfile.ML_CS,
        abstract=(
            "We trained on N = 5000 patients and found t(48) = 0.40, p = 0.008 "
            "across benchmark tests without confidence intervals."
        ),
        full_text="accuracy 99.1% on benchmark.",
        artifacts=ArtifactManifest(
            links=[
                ArtifactLink(
                    url="https://github.com/org/flawed",
                    host="github",
                    kind="code",
                    status=ArtifactStatus.RESOLVED,
                    local_path=str(tmp_path),
                ),
                ArtifactLink(
                    url="https://zenodo.org/records/1",
                    host="zenodo",
                    kind="data",
                    status=ArtifactStatus.RESOLVED,
                    local_path=str(tmp_path),
                ),
            ],
            code_files=["prep.py"],
            data_files=["data.csv"],
        ),
    )

    report = evaluate_stage0_to_3(paper, workdir=tmp_path)
    by_id = {r.gate_id: r for r in report.gate_results}

    assert by_id["G3.6_LEAKAGE_HYGIENE"].status == GateStatus.FAIL
    assert by_id["G3.7_DATA_READABLE_MATCHES_N"].status == GateStatus.FAIL
    assert by_id["G3.8_EXCLUSIONS_DOCUMENTED"].status == GateStatus.FAIL
    assert by_id["G3.9_NUMERIC_CONSISTENCY"].status == GateStatus.FAIL
    assert len(report.human_review_flags) >= 4


def test_stage0_to_3_with_mocked_gemini_client_records_model_provenance(
    tmp_path: Path,
) -> None:
    paper = _create_well_packaged_workspace(tmp_path)
    client = GeminiClient(
        config=GeminiConfig(api_key="test-key", cache_path=tmp_path / "cache.sqlite"),
        transport=lambda *_: (
            '{"passed": true, "confidence": 0.95, "rationale": "Verified by LLM.", '
            '"evidence_source": "abstract", "evidence_excerpt": "N = 100", "flags": []}'
        ),
    )

    report = evaluate_stage0_to_3(paper, workdir=tmp_path, llm=client)
    by_id = {r.gate_id: r for r in report.gate_results}
    assert by_id["G0.1_EMPIRICAL_CLAIM"].model_id == client.config.fast_model
    assert by_id["G3.4_EFFECT_SIZES_UNCERTAINTY"].model_id == client.config.reasoning_model


def test_cli_audit_command_runs_offline_and_writes_outputs(tmp_path: Path) -> None:
    paper = _create_well_packaged_workspace(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    out_json = tmp_path / "audit.json"
    out_md = tmp_path / "dossier.md"
    bundle_path.write_text(paper.model_dump_json(), encoding="utf-8")

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "audit",
            str(bundle_path),
            "--workdir",
            str(tmp_path),
            "--offline",
            "--output",
            str(out_json),
            "--markdown",
            str(out_md),
        ],
    )
    assert res.exit_code == 0
    assert out_json.exists()
    assert out_md.exists()
    assert "# Reproducibility Audit: arxiv:2502.00001" in out_md.read_text(encoding="utf-8")
