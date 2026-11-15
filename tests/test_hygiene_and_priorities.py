"""Tests for I4R-derived top-6 reproducibility priorities across Stages 2, 3, and 5."""

from __future__ import annotations

import json
from pathlib import Path

from researchability.gates.base import EvaluationContext
from researchability.gates.stage2 import (
    eval_g2_1_readme_entrypoint,
    eval_g2_2_env_manifest,
    eval_g2_3_versions_pinned,
)
from researchability.gates.stage3_integrity import (
    eval_g3_7_data_readable_matches_n,
    eval_g3_8_exclusions_documented,
    eval_g3_9_numeric_consistency,
)
from researchability.gates.stage5_verification import (
    eval_g5_1_numeric_outcomes_match,
    render_human_dossier,
)
from researchability.models import (
    ArtifactLink,
    ArtifactManifest,
    ArtifactStatus,
    AuditReport,
    FieldProfile,
    GateStatus,
    PaperBundle,
)
from researchability.profiler.data_profiler import profile_data_file
from researchability.sandbox import CommandResult, SandboxRunResult


def _bundle(
    tmp_path: Path,
    *,
    code_files: list[str] | None = None,
    data_files: list[str] | None = None,
    readme_files: list[str] | None = None,
    manifest_files: list[str] | None = None,
    full_text: str = "We analyze N = 6 observations.",
) -> tuple[PaperBundle, EvaluationContext]:
    paper = PaperBundle(
        paper_id="i4r_test",
        title="I4R Test Study",
        abstract="We evaluate empirical models on N = 6 rows.",
        full_text=full_text,
        source_repo="https://example.org/repo",
        primary_profile=FieldProfile.ECONOMICS,
        artifacts=ArtifactManifest(
            code_files=code_files or [],
            data_files=data_files or [],
            readme_files=readme_files or [],
            manifest_files=manifest_files or [],
            links=[
                ArtifactLink(
                    url="https://example.org/repo",
                    kind="code",
                    status=ArtifactStatus.RESOLVED,
                )
            ],
        ),
    )
    return paper, EvaluationContext(paper=paper, workdir=tmp_path)


# ---------------------------------------------------------------------------
# Priority 1: README-to-Repo Inventory Diff, Duplicate Files & Output Collisions
# ---------------------------------------------------------------------------


def test_g2_1_catches_stale_readme_refs_divergent_duplicates_and_output_collisions(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text(
        "Run `python run_v2.py` to process `missing_archive.csv`.\n",
        encoding="utf-8",
    )
    (tmp_path / "run_FINAL.py").write_text(
        "import pandas as pd\ndf.to_csv('results/table1.csv')\n",
        encoding="utf-8",
    )
    (tmp_path / "run_v2.py").write_text(
        "import pandas as pd\ndf.to_csv('results/table1.csv')\n",
        encoding="utf-8",
    )
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "dat_long.csv").write_text("id,x\n1,10\n", encoding="utf-8")
    (tmp_path / "b" / "dat_long.csv").write_text("id,x\n1,99\n", encoding="utf-8")

    _, ctx = _bundle(
        tmp_path,
        readme_files=["README.md"],
        code_files=["run_FINAL.py", "run_v2.py"],
        data_files=["a/dat_long.csv", "b/dat_long.csv"],
    )
    res = eval_g2_1_readme_entrypoint(ctx)
    assert res.status == GateStatus.FAIL
    assert any(f.startswith("STALE_README_FILE_REF") for f in res.flags)
    assert any(f.startswith("DUPLICATE_DIVERGENT_FILE") for f in res.flags)
    assert any(f.startswith("OUTPUT_COLLISION") for f in res.flags)
    assert any(f.startswith("COMPETING_ENTRYPOINTS") for f in res.flags)


def test_g2_1_rejects_binary_only_readme(tmp_path: Path) -> None:
    (tmp_path / "Readme.docx").write_bytes(b"PK\x03\x04binary-docx-payload python main.py")
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    _, ctx = _bundle(tmp_path, readme_files=["Readme.docx"], code_files=["main.py"])
    res = eval_g2_1_readme_entrypoint(ctx)
    assert res.status == GateStatus.FAIL
    assert any(f.startswith("NON_PLAIN_TEXT_README") for f in res.flags)


# ---------------------------------------------------------------------------
# Priority 2: Sentinel Missing-Value & Negative-Code Contamination Audit
# ---------------------------------------------------------------------------


def test_sentinel_missing_codes_empty_column_and_unguarded_filter_fail(tmp_path: Path) -> None:
    csv_path = tmp_path / "cps_subset.csv"
    csv_path.write_text(
        "id,hufaminc,temp,all_empty\n"
        "1,-1,-999,\n"
        "2,-2,10,\n"
        "3,-3,12,\n"
        "4,15000,14,\n"
        "5,25000,18,\n"
        "6,45000,20,\n",
        encoding="utf-8",
    )
    (tmp_path / "clean.py").write_text(
        "import pandas as pd\n"
        "df = pd.read_csv('cps_subset.csv')\n"
        "low_income = df[df['hufaminc'] < 20000]\n"
        "df['temp'] = df['temp'].fillna(0)\n",
        encoding="utf-8",
    )
    # Match regex variable syntax `hufaminc < 20000`
    (tmp_path / "clean_r.R").write_text(
        "df <- read.csv('cps_subset.csv')\nlow_income <- subset(df, hufaminc < 20000)\n",
        encoding="utf-8",
    )

    prof = profile_data_file(csv_path)
    assert "hufaminc" in prof.sentinel_columns
    assert "temp" in prof.sentinel_columns
    assert prof.empty_columns == ["all_empty"]

    _, ctx = _bundle(
        tmp_path,
        code_files=["clean.py", "clean_r.R"],
        data_files=["cps_subset.csv"],
        full_text="We study N = 6 CPS households.",
    )
    g37 = eval_g3_7_data_readable_matches_n(ctx)
    assert g37.status == GateStatus.FAIL
    assert any("SENTINEL_MISSING_CODES" in f for f in g37.flags)
    assert any("EMPTY_DATA_COLUMN" in f for f in g37.flags)
    assert any("UNGUARDED_SENTINEL_FILTER" in f for f in g37.flags)

    g38 = eval_g3_8_exclusions_documented(ctx)
    assert g38.status == GateStatus.FAIL
    assert any("UNGUARDED_SENTINEL_FILTER" in f for f in g38.flags)
    assert any("BLIND_NA_TO_ZERO" in f for f in g38.flags)


# ---------------------------------------------------------------------------
# Priority 3: Table Star / CI / Effect Bound & GRIM Wired into G3.9
# ---------------------------------------------------------------------------


def test_g3_9_surfaces_table_stars_cis_effect_bounds_and_grim(tmp_path: Path) -> None:
    _, ctx = _bundle(
        tmp_path,
        full_text=(
            "Baseline sample mean = 3.55, N = 10. "
            "Model estimates: b1 = 0.04*** (0.25), b2 = 0.90 (0.15), "
            "Cohen's d = 3.80, Pearson r = -1.12, beta = 0.50, 95% CI [0.10, 0.30]."
        ),
    )
    g39 = eval_g3_9_numeric_consistency(ctx)
    assert g39.status == GateStatus.FAIL
    assert any("GRIM inconsistency" in f for f in g39.flags)
    assert any("Significance star mismatch" in f for f in g39.flags)
    assert any("Implausibly large standardized effect size" in f for f in g39.flags)
    assert any("Out-of-range correlation" in f for f in g39.flags)
    assert any("Confidence interval mismatch" in f for f in g39.flags)


# ---------------------------------------------------------------------------
# Priority 4: Package Registry Liveness & Static File Cross-Ref Preflight
# ---------------------------------------------------------------------------


def test_g2_2_g2_3_undeclared_unpinned_remote_and_mocked_archived_packages(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("numpy==1.26.4\n", encoding="utf-8")
    (tmp_path / "run.py").write_text(
        "import numpy as np\nimport phantom_missing_pkg\n",
        encoding="utf-8",
    )
    (tmp_path / "install.R").write_text(
        "library(reproducibleDeadPkg)\ndevtools::install_github('author/myUnpinnedPkg')\n",
        encoding="utf-8",
    )
    _, ctx = _bundle(
        tmp_path,
        manifest_files=["requirements.txt"],
        code_files=["run.py", "install.R"],
    )

    def fake_http_get(url: str) -> tuple[int, str]:
        if "phantom_missing_pkg" in url:
            return 404, "Not Found"
        if "reproducibleDeadPkg" in url:
            return 200, '{"Package":"reproducibleDeadPkg","Repository":"Archived"}'
        return 200, "{}"

    ctx.extracted_claims["check_registry"] = True
    ctx.extracted_claims["registry_http_get"] = fake_http_get

    g22 = eval_g2_2_env_manifest(ctx)
    assert g22.status == GateStatus.PASS
    assert any("UNDECLARED_IMPORTS" in f for f in g22.flags)

    g23 = eval_g2_3_versions_pinned(ctx)
    assert g23.status == GateStatus.FAIL
    assert any("UNPINNED_NON_CRAN_PACKAGE" in f for f in g23.flags)
    assert any("ARCHIVED_OR_MISSING_PACKAGE" in f for f in g23.flags)
    assert "pypi:phantom_missing_pkg" in " ".join(g23.flags)
    assert "cran:reproducibleDeadPkg (archived)" in " ".join(g23.flags)


def test_g2_1_flags_missing_script_input_and_hardcoded_setwd(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Run `Rscript main.R`.\n", encoding="utf-8")
    (tmp_path / "main.R").write_text(
        'setwd("C:/Users/author/Dropbox/paper")\ndf <- readRDS("data_all_waves.rds")\n',
        encoding="utf-8",
    )
    _, ctx = _bundle(tmp_path, readme_files=["README.md"], code_files=["main.R"])
    res = eval_g2_1_readme_entrypoint(ctx)
    assert any(f.startswith("MISSING_REFERENCED_FILE") for f in res.flags)
    assert any(f.startswith("HARDCODED_LOCAL_PATH") for f in res.flags)


# ---------------------------------------------------------------------------
# Priority 5: Post-Run Log Audits (Convergence, Dropped Singletons, Model N_obs)
# ---------------------------------------------------------------------------


def test_g5_1_and_dossier_surface_convergence_singletons_and_effective_n_mismatch(
    tmp_path: Path,
) -> None:
    metrics_path = tmp_path / "metrics.json"
    metrics_path.write_text(json.dumps({"accuracy": 0.912, "nobs": 740}), encoding="utf-8")
    log_path = tmp_path / "stata_run.log"
    log_text = (
        "(MWFE estimator converged in 4 iterations)\n"
        "note: 120 singleton observations were dropped\n"
        "Warning: model did not converge; boundary (singular) fit\n"
        "Number of obs = 740\n"
    )
    log_path.write_text(log_text, encoding="utf-8")

    paper, ctx = _bundle(
        tmp_path,
        full_text=(
            "Our main regression analyzes N = 1,000 observations and yields Accuracy = 0.912."
        ),
    )
    ctx.extracted_claims["analysis_run_effective"] = SandboxRunResult(
        success=True,
        image="test:latest",
        command=["bash", "run.sh"],
        command_result=CommandResult(
            command=["bash", "run.sh"],
            returncode=0,
            stdout=log_text,
            stderr="",
        ),
        created_files=["metrics.json", "stata_run.log"],
        output_previews={"metrics.json": metrics_path.read_text(), "stata_run.log": log_text},
    )

    g51 = eval_g5_1_numeric_outcomes_match(ctx)
    assert g51.status == GateStatus.PASS
    assert any(f.startswith("CONVERGENCE_WARNING") for f in g51.flags)
    assert any(f.startswith("DROPPED_SINGLETONS") for f in g51.flags)
    assert any(f.startswith("EFFECTIVE_N_MISMATCH") for f in g51.flags)

    report = AuditReport(
        paper_id=paper.paper_id,
        source_repo=paper.source_repo,
        profile=paper.primary_profile,
        funnel_depth=5,
        gate_results=[g51],
        human_review_flags=list(g51.flags),
    )
    dossier = render_human_dossier(
        report,
        paper=paper,
        comparisons=ctx.extracted_claims["numeric_comparisons"],
        execution_log_audit=ctx.extracted_claims["execution_log_audit"],
    )
    assert "## Execution Log & Effective-N Audit (G5.1)" in dossier
    assert "Dropped singletons" in dossier
    assert "Effective-N mismatch" in dossier


# ---------------------------------------------------------------------------
# Priority 6: Public PII / Participant Identifier Scan
# ---------------------------------------------------------------------------


def test_g3_7_detects_pii_leaks_in_csv_and_jsonl(tmp_path: Path) -> None:
    (tmp_path / "participants.csv").write_text(
        "id,respondent_name,score\n1,alice@university.edu,88\n2,bob@example.org,92\n",
        encoding="utf-8",
    )
    (tmp_path / "events.jsonl").write_text(
        '{"id": 1, "ip_address": "192.168.10.22", "ok": 1}\n'
        '{"id": 2, "ip_address": "10.0.0.5", "ok": 0}\n',
        encoding="utf-8",
    )

    _, ctx = _bundle(
        tmp_path,
        data_files=["participants.csv", "events.jsonl"],
        full_text="We evaluate N = 2 subjects.",
    )
    res = eval_g3_7_data_readable_matches_n(ctx)
    assert res.status == GateStatus.FAIL
    assert any("PII_LEAK_DETECTED" in f and "Email" in f for f in res.flags)
    assert any("PII_LEAK_DETECTED" in f and "IPv4" in f for f in res.flags)
    assert any("PII_LEAK_DETECTED" in f and "ip_address" in f for f in res.flags)
