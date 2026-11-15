"""Tests covering remaining Milestone 7 I4R-derived checks (TRE-107..TRE-122)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from researchability.gates.base import EvaluationContext
from researchability.gates.stage0_1 import (
    eval_g1_5_data_prep_code,
    eval_g1_7_preregistration_disclosed,
)
from researchability.gates.stage2 import eval_g2_1_readme_entrypoint
from researchability.gates.stage3_design_stats import eval_g3_6_leakage_hygiene
from researchability.gates.stage3_integrity import eval_g3_7_data_readable_matches_n
from researchability.gates.stage4_execution import (
    eval_g4_1_sandbox_feasible,
    eval_g4_4_analysis_zero_touch,
)
from researchability.gates.stage5_verification import (
    eval_g5_1_numeric_outcomes_match,
    eval_g5_2_figures_regenerate,
    eval_g5_3_human_dossier_ready,
    render_human_dossier,
)
from researchability.gates.stage_crossfield import (
    eval_g1_10_psych_pap_adherence,
    eval_g3_10_econ_robustness_specification,
)
from researchability.models import (
    ArtifactLink,
    ArtifactManifest,
    AuditReport,
    FieldProfile,
    GateStatus,
    PaperBundle,
)
from researchability.sandbox import (
    CommandResult,
    DockerSandboxRunner,
    SandboxConfig,
    SandboxRunResult,
)


def _make_ctx(
    tmp_path: Path,
    *,
    code_files: list[str] | None = None,
    data_files: list[str] | None = None,
    readme_files: list[str] | None = None,
    links: list[ArtifactLink] | None = None,
    full_text: str = "We study N = 20 observations.",
    profile: FieldProfile = FieldProfile.ECONOMICS,
) -> tuple[PaperBundle, EvaluationContext]:
    paper = PaperBundle(
        paper_id="m7_test",
        title="M7 Check Study",
        abstract="Sample N = 20.",
        full_text=full_text,
        source_repo="https://example.org/repo",
        primary_profile=profile,
        artifacts=ArtifactManifest(
            code_files=code_files or [],
            data_files=data_files or [],
            readme_files=readme_files or [],
            links=links or [],
        ),
    )
    return paper, EvaluationContext(paper=paper, workdir=tmp_path)


def test_g1_5_processed_data_without_prep_lineage_fails(tmp_path: Path) -> None:
    (tmp_path / "cleaned_panel.csv").write_text("id,y\n1,2\n", encoding="utf-8")
    (tmp_path / "01_models.R").write_text(
        "df <- read.csv('cleaned_panel.csv')\nfit <- lm(y ~ x, data = df)\n",
        encoding="utf-8",
    )
    _, ctx = _make_ctx(
        tmp_path,
        code_files=["01_models.R"],
        data_files=["cleaned_panel.csv"],
    )
    res = eval_g1_5_data_prep_code(ctx)
    assert res.status == GateStatus.FAIL
    assert any("PROCESSED_DATA_ONLY_NO_RAW_LINEAGE" in f for f in res.flags)


def test_g3_7_catches_missing_gis_sidecars_and_spatial_assets(tmp_path: Path) -> None:
    (tmp_path / "regions.shp").write_bytes(b"SHP")
    (tmp_path / "cohort.csv").write_text("id,x\n1,10\n", encoding="utf-8")
    (tmp_path / "map.R").write_text(
        "shp <- st_read('regions.shp')\ngeo <- st_read('missing_map.geojson')\n",
        encoding="utf-8",
    )
    _, ctx = _make_ctx(
        tmp_path,
        code_files=["map.R"],
        data_files=["regions.shp", "cohort.csv"],
        full_text="We study N = 1 observation.",
    )
    res = eval_g3_7_data_readable_matches_n(ctx)
    assert res.status == GateStatus.FAIL
    assert any("MISSING_GIS_SIDECARS" in f for f in res.flags)
    assert any("MISSING_SPATIAL_OR_BINARY_ASSET" in f for f in res.flags)


def test_g2_1_and_g3_7_catch_stata_hygiene_magic_constants_log1p_zeros_and_missing_col(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("Run `01_prep.do` then `02_models.do`.\n", encoding="utf-8")
    (tmp_path / "01_prep.do").write_text("use raw.dta\nsave panel.dta, replace\n", encoding="utf-8")
    (tmp_path / "02_models.do").write_text(
        "sort state\nreghdfe y x, absorb(id)\n* filter balance ip_addr\n",
        encoding="utf-8",
    )
    args_12 = ", ".join(f"a{i}" for i in range(12))
    (tmp_path / "03_plot.R").write_text(
        f"plot_grid({args_12})\n"
        "m1 <- lm(y ~ x, data = df)\n"
        "m1 <- lm(y ~ z, data = df)\n"
        "vals <- c(0.58, 0.86, 0.59, 0.72)\n"
        "y2 <- log1p(y_sparse)\n"
        "bad <- df$nonexistent_col\n",
        encoding="utf-8",
    )
    rows = "\n".join(f"{i},{0 if i < 8 else i * 5}" for i in range(10))
    (tmp_path / "panel.csv").write_text(f"id,y_sparse\n{rows}\n", encoding="utf-8")

    _, ctx = _make_ctx(
        tmp_path,
        readme_files=["README.md"],
        code_files=["01_prep.do", "02_models.do", "03_plot.R"],
        data_files=["panel.csv"],
        full_text="We study N = 10 observations.",
    )
    g21 = eval_g2_1_readme_entrypoint(ctx)
    assert any("STATA_HYGIENE_RISK" in f for f in g21.flags)
    assert any("IMPLICIT_GLOBAL_STATE" in f for f in g21.flags)
    assert any("MODEL_OBJECT_OVERWRITE" in f for f in g21.flags)
    assert any("EXCESSIVE_POSITIONAL_ARGS" in f for f in g21.flags)
    assert any("HARDCODED_MAGIC_CONSTANT" in f for f in g21.flags)
    assert any("COMMENTED_OUT_DIAGNOSTIC_CHECK" in f for f in g21.flags)

    g37 = eval_g3_7_data_readable_matches_n(ctx)
    assert any("LOG1P_HIGH_ZERO_MASS" in f for f in g37.flags)
    assert any("MISSING_REFERENCED_COLUMN" in f for f in g37.flags)


def test_g3_6_and_g3_10_catch_weight_clustering_and_year_spec_mismatches(tmp_path: Path) -> None:
    (tmp_path / "models.R").write_text(
        "sub <- df[df$year <= 2018 & df$x > mean(df$x), ]\n"
        "m1 <- feols(y ~ treat, weights = ~wt, cluster = ~state, data = sub)\n"
        "m2 <- feols(y ~ treat, data = sub)\n",
        encoding="utf-8",
    )
    _, ctx = _make_ctx(
        tmp_path,
        code_files=["models.R"],
        full_text=("Our placebo test uses a median split across the 2013-2019 period (N = 20)."),
    )
    g36 = eval_g3_6_leakage_hygiene(ctx)
    assert g36.status == GateStatus.FAIL
    assert any("INCONSISTENT_WEIGHTS_OR_SE" in f for f in g36.flags)
    assert any("SPECIFICATION_MISMATCH" in f for f in g36.flags)

    g310 = eval_g3_10_econ_robustness_specification(ctx)
    assert g310.status == GateStatus.FAIL
    assert any("SPECIFICATION_MISMATCH" in f for f in g310.flags)


def test_g1_7_and_g1_10_fetch_prereg_and_flag_deviations(tmp_path: Path) -> None:
    _, ctx = _make_ctx(
        tmp_path,
        links=[ArtifactLink(url="https://osf.io/abc12", kind="prereg", host="osf")],
        profile=FieldProfile.PSYCHOLOGY_SOCIAL,
        full_text="We evaluate N = 120 participants on secondary recall accuracy.",
    )
    ctx.extracted_claims["check_prereg_registry"] = True
    ctx.extracted_claims["prereg_http_get"] = lambda _url: (
        200,
        json.dumps(
            {
                "data": {
                    "attributes": {
                        "title": "Planned Trial",
                        "description": "Target N = 300. Primary outcome is Reaction Time Index.",
                    }
                }
            }
        ),
    )
    g17 = eval_g1_7_preregistration_disclosed(ctx)
    assert g17.status == GateStatus.PASS
    assert any("PAP_SAMPLE_SIZE_DEVIATION" in f for f in g17.flags)
    assert any("PAP_OUTCOME_OMISSION" in f for f in g17.flags)

    g110 = eval_g1_10_psych_pap_adherence(ctx)
    assert g110.status == GateStatus.FAIL
    assert any("PAP_SAMPLE_SIZE_DEVIATION" in f for f in g110.flags)


def test_g5_2_table_figure_coverage_and_g5_3_quantitative_perturbations(tmp_path: Path) -> None:
    (tmp_path / "make_figs.R").write_text(
        "ggsave('output/figure1.png')\nesttab using 'output/table1.tex', replace\n",
        encoding="utf-8",
    )
    rows = "\n".join(
        [
            "treat,outcome",
            *[(f"{i % 2},{10 + 2 * (i % 2)}") for i in range(19)],
            "1,-5000",
        ]
    )
    (tmp_path / "reg_data.csv").write_text(rows + "\n", encoding="utf-8")
    fig1 = tmp_path / "output"
    fig1.mkdir()
    (fig1 / "figure1.png").write_bytes(b"PNG")

    paper, ctx = _make_ctx(
        tmp_path,
        code_files=["make_figs.R"],
        data_files=["reg_data.csv"],
        full_text=(
            "Table 1 shows main results (p = 0.032, p = 0.041, p = 0.048). "
            "Figure 1 shows trends, while Figure 4 reports additional breakdowns (N = 20)."
        ),
    )
    ctx.extracted_claims["analysis_run_effective"] = SandboxRunResult(
        success=True,
        image="test:latest",
        command=["Rscript", "make_figs.R"],
        command_result=CommandResult(command=["Rscript", "make_figs.R"], returncode=0),
        created_files=["output/figure1.png"],
    )
    g52 = eval_g5_2_figures_regenerate(ctx)
    assert g52.status == GateStatus.PASS
    assert any("UNCOVERED_PAPER_TABLE_OR_FIGURE" in f and "figure_4" in f for f in g52.flags)

    g53 = eval_g5_3_human_dossier_ready(ctx)
    assert g53.status == GateStatus.PASS
    assert any("BONFERRONI_SIGNIFICANCE_REVERSAL" in f for f in g53.flags)
    assert any("OUTLIER_SENSITIVITY_FLIP" in f for f in g53.flags)

    report = AuditReport(
        paper_id=paper.paper_id,
        source_repo=paper.source_repo,
        profile=paper.primary_profile,
        funnel_depth=5,
        gate_results=[g52, g53],
        human_review_flags=[*g52.flags, *g53.flags],
    )
    dossier = render_human_dossier(
        report,
        paper=paper,
        table_figure_coverage=ctx.extracted_claims["table_figure_coverage"],
        quantitative_robustness=ctx.extracted_claims["quantitative_robustness"],
    )
    assert "## Table & Figure Coverage Audit (G5.2)" in dossier
    assert "## Quantitative Robustness Perturbations (G5.3)" in dossier


def test_g4_1_subsampled_fallback_and_multiseed_instability(tmp_path: Path) -> None:
    class _FakeSandbox(DockerSandboxRunner):
        def __init__(self) -> None:
            super().__init__(config=SandboxConfig(max_data_bytes=10))
            self._calls = 0

        def image_tag_for(self, paper: PaperBundle) -> str:
            _ = paper
            return "test:latest"

        def run_command(
            self,
            image_tag: str,
            cmd: list[str],
            workdir: Path,
            data_mount: Path | None = None,
            env: dict[str, str] | None = None,
        ) -> SandboxRunResult:
            _ = (image_tag, workdir, data_mount)
            self._calls += 1
            seed = int((env or {}).get("RESEARCHABILITY_SEED", "1"))
            val = 0.90 if seed == 1 else (-0.25 if seed == 202 else 0.40)
            return SandboxRunResult(
                success=True,
                image="test:latest",
                command=cmd,
                command_result=CommandResult(
                    command=cmd,
                    returncode=0,
                    stdout=f"Accuracy = {val}\n",
                ),
            )

    (tmp_path / "big.csv").write_text("x\n1\n2\n3\n4\n5\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print('Accuracy = 0.90')\n", encoding="utf-8")
    _, ctx = _make_ctx(
        tmp_path,
        code_files=["main.py"],
        data_files=["big.csv"],
        full_text="We achieve Accuracy = 0.90 across N = 5 samples.",
    )
    ctx.extracted_claims["sandbox_runner"] = _FakeSandbox()
    ctx.extracted_claims["subsampled_reference_vals"] = [1.0, 2.0, 3.0, 4.0, 5.0]
    ctx.extracted_claims["subsampled_candidate_vals"] = [1.1, 2.0, 2.9, 4.1, 4.9]

    g41 = eval_g4_1_sandbox_feasible(ctx)
    assert g41.status == GateStatus.BLOCKED_DATA_SIZE
    assert any("SUBSAMPLED_DISTRIBUTION_VERIFIED" in f for f in g41.flags)

    ctx.extracted_claims["multiseed_values"] = [101, 202, 303]
    g44 = eval_g4_4_analysis_zero_touch(ctx)
    assert g44.status == GateStatus.PASS
    g51 = eval_g5_1_numeric_outcomes_match(ctx)
    assert g51.status == GateStatus.PASS
    assert any("MULTISEED_SIGN_FLIP" in f or "MULTISEED_INSTABILITY" in f for f in g51.flags)

    # Keep mypy clean with explicit Any use
    _dummy: Any = g41
    assert _dummy.gate_id == "G4.1_SANDBOX_FEASIBLE"
