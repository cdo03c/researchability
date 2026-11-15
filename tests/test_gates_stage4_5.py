"""End-to-end tests for Stage 4 execution gates, Stage 5 output verification, and dossiers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from researchability.cli import app
from researchability.gates import (
    ALL_STAGE0_TO_5_EVALUATORS,
    evaluate_stage0_to_5_with_context,
)
from researchability.models import (
    ArtifactManifest,
    FieldProfile,
    GateStatus,
    PaperBundle,
)
from researchability.sandbox import CommandResult, DockerSandboxRunner, SandboxConfig


def _extract_workspace_from_docker_args(args: list[str]) -> Path | None:
    for idx, token in enumerate(args):
        if token == "-v" and idx + 1 < len(args):
            mount = args[idx + 1]
            if mount.endswith(":/workspace"):
                return Path(mount[: -len(":/workspace")])
    return None


def _python_executing_sandbox_runner(
    *,
    fail_build: bool = False,
    script_override: dict[str, CommandResult] | None = None,
) -> DockerSandboxRunner:
    """Create a DockerSandboxRunner that executes Python scripts locally for deterministic tests."""

    def fake_runner(
        args: list[str], *, cwd: Path | None = None, timeout: int = 300
    ) -> CommandResult:
        if args[1] == "build":
            if fail_build:
                return CommandResult(returncode=1, stderr="pip resolution error", command=args)
            return CommandResult(returncode=0, stdout="built", command=args)

        workspace = _extract_workspace_from_docker_args(args) or cwd
        assert workspace is not None
        # Find image tag index followed by command
        if "python" in args:
            py_idx = args.index("python")
            cmd = [sys.executable, *args[py_idx + 1 :]]
        else:
            cmd = args[-2:]

        if script_override and args[-1] in script_override:
            return script_override[args[-1]]

        env_vars = None
        for idx, token in enumerate(args):
            if token == "-e" and idx + 1 < len(args) and "=" in args[idx + 1]:
                import os

                if env_vars is None:
                    env_vars = dict(os.environ)
                k, v = args[idx + 1].split("=", 1)
                env_vars[k] = v

        completed = subprocess.run(
            cmd,
            cwd=str(workspace),
            env=env_vars,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=args,
        )

    return DockerSandboxRunner(config=SandboxConfig(), runner=fake_runner)


def _complete_ml_paper(
    *,
    paper_id: str = "arxiv:2401.99901",
    code_files: list[str] | None = None,
    accuracy: float = 0.912,
    f1_pct: float = 88.4,
) -> PaperBundle:
    methods_and_results = (
        "Pretrained transformer baseline evaluated on held-out test set. "
        "This exploratory study (not preregistered) tests classification robustness. "
        "We set random seed 42 on an NVIDIA A100 GPU (with CPU fallback) and perform "
        "5-fold cross-validation plus ablation and sensitivity analysis over learning rate. "
        "Power analysis and compute budget indicate sample size n = 4 synthetic benchmark rows. "
        f"Accuracy = {accuracy} and F1-score = {f1_pct}%, 95% CI [0.87, 0.95], "
        "with Bonferroni correction (p = 0.002). "
        "Limitations and failure cases include domain shift on noisy text. "
        "Preprocessing uses fit-on-train only without leakage. See Table 1 and Figure 1."
    )
    return PaperBundle(
        paper_id=paper_id,
        source_repo="arxiv",
        title="Reproducible Classifier Study",
        authors=["Ada Lovelace"],
        published_year=2024,
        primary_profile=FieldProfile.ML_CS,
        abstract=methods_and_results,
        full_text=methods_and_results,
        sections={"Methods": methods_and_results, "Results": methods_and_results},
        artifacts=ArtifactManifest(
            code_files=code_files or ["prep.py", "main.py"],
            data_files=["data/samples.csv"],
            manifest_files=["requirements.txt"],
            readme_files=["README.md"],
        ),
        metadata={"code_availability_statement": True},
    )


def test_stage4_and_5_evaluators_registered() -> None:
    expected = {
        "G4.1_SANDBOX_FEASIBLE",
        "G4.2_ENV_BUILDS",
        "G4.3_PREP_EXECUTES",
        "G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH",
        "G4.5_ANALYSIS_EXECUTES_REPAIRED",
        "G5.1_NUMERIC_OUTCOMES_MATCH",
        "G5.2_FIGURES_REGENERATE",
        "G5.3_HUMAN_DOSSIER_READY",
    }
    assert expected.issubset(ALL_STAGE0_TO_5_EVALUATORS.keys())


def test_end_to_end_zero_touch_reproducible_paper(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "samples.csv").write_text(
        "x,label\n1,1\n2,0\n3,1\n4,0\n", encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text("numpy==1.26.4\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "# Repro Repo\nRun `python prep.py` then `python main.py`.\n",
        encoding="utf-8",
    )
    (tmp_path / "prep.py").write_text(
        "from pathlib import Path\nPath('clean.csv').write_text('x,y\\n1,1\\n2,0\\n')\n",
        encoding="utf-8",
    )
    (tmp_path / "main.py").write_text(
        "import json, random\n"
        "from pathlib import Path\n"
        "random.seed(42)\n"
        "Path('metrics.json').write_text(json.dumps({'accuracy': 0.912, 'f1_score': 0.884}))\n"
        "Path('figure1.png').write_bytes(b'PNGDATA')\n"
        "print('accuracy = 0.912')\n",
        encoding="utf-8",
    )

    paper = _complete_ml_paper()
    sandbox = _python_executing_sandbox_runner()
    report, ctx = evaluate_stage0_to_5_with_context(paper, workdir=tmp_path, sandbox=sandbox)

    by_id = {res.gate_id: res for res in report.gate_results}
    assert by_id["G4.1_SANDBOX_FEASIBLE"].status == GateStatus.PASS
    assert by_id["G4.2_ENV_BUILDS"].status == GateStatus.PASS
    assert by_id["G4.3_PREP_EXECUTES"].status == GateStatus.PASS
    assert by_id["G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH"].status == GateStatus.PASS
    assert by_id["G4.5_ANALYSIS_EXECUTES_REPAIRED"].status == GateStatus.N_A
    assert by_id["G5.1_NUMERIC_OUTCOMES_MATCH"].status == GateStatus.PASS
    assert by_id["G5.2_FIGURES_REGENERATE"].status == GateStatus.PASS
    assert by_id["G5.3_HUMAN_DOSSIER_READY"].status == GateStatus.PASS

    assert report.zero_touch_reproducible is True
    assert report.repaired_reproducible is True
    assert report.funnel_depth == 5

    dossier = str(ctx.extracted_claims["human_dossier_markdown"])
    assert "Numeric Outcome Comparison (G5.1)" in dossier
    assert "figure1.png" in dossier
    assert "Suggested Human Robustness Checks" in dossier


def test_end_to_end_repaired_reproducible_logs_unified_diff(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "samples.csv").write_text("x,y\n1,1\n2,0\n3,1\n4,0\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("numpy==1.26.4\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Repo\nRun `python main.py`.\n", encoding="utf-8")
    (tmp_path / "prep.py").write_text("print('prep ok')\n", encoding="utf-8")
    # Intentional hardcoded author path + missing output directory
    (tmp_path / "main.py").write_text(
        "import json, random\n"
        "from pathlib import Path\n"
        "random.seed(42)\n"
        "raw = Path('/Users/author/project/samples.csv').read_text()\n"
        "Path('outputs/results.csv').write_text('metric,value\\naccuracy,0.911\\nf1,0.884\\n')\n"
        "Path('outputs/fig1.svg').write_text('<svg></svg>')\n",
        encoding="utf-8",
    )

    paper = _complete_ml_paper()
    sandbox = _python_executing_sandbox_runner()
    report, ctx = evaluate_stage0_to_5_with_context(paper, workdir=tmp_path, sandbox=sandbox)

    by_id = {res.gate_id: res for res in report.gate_results}
    assert by_id["G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH"].status == GateStatus.FAIL
    assert by_id["G4.5_ANALYSIS_EXECUTES_REPAIRED"].status == GateStatus.PASS
    assert by_id["G5.1_NUMERIC_OUTCOMES_MATCH"].status == GateStatus.PASS
    assert by_id["G5.2_FIGURES_REGENERATE"].status == GateStatus.PASS
    assert by_id["G5.3_HUMAN_DOSSIER_READY"].status == GateStatus.PASS

    assert report.zero_touch_reproducible is False
    assert report.repaired_reproducible is True
    repair_ev = by_id["G4.5_ANALYSIS_EXECUTES_REPAIRED"].evidence
    assert repair_ev and repair_ev[0].note and "--- a/main.py" in repair_ev[0].note

    dossier = str(ctx.extracted_claims["human_dossier_markdown"])
    assert "Logged Mechanical Repair Diffs (G4.5)" in dossier
    assert "--- a/main.py" in dossier


def test_numeric_mismatch_fails_g5_1_with_diff_flag(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "samples.csv").write_text("x,y\n1,1\n2,0\n3,1\n4,0\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("numpy==1.26.4\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Repo\nRun `python main.py`.\n", encoding="utf-8")
    (tmp_path / "prep.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "import json, random\n"
        "from pathlib import Path\n"
        "random.seed(42)\n"
        "Path('metrics.json').write_text(json.dumps({'accuracy': 0.54, 'f1_score': 0.51}))\n"
        "Path('table1.csv').write_text('metric,value\\naccuracy,0.54\\n')\n",
        encoding="utf-8",
    )

    paper = _complete_ml_paper(accuracy=0.912, f1_pct=88.4)
    sandbox = _python_executing_sandbox_runner()
    report, _ = evaluate_stage0_to_5_with_context(paper, workdir=tmp_path, sandbox=sandbox)

    by_id = {res.gate_id: res for res in report.gate_results}
    assert by_id["G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH"].status == GateStatus.PASS
    assert by_id["G5.1_NUMERIC_OUTCOMES_MATCH"].status == GateStatus.FAIL
    assert "NUMERIC_OUTCOME_MISMATCH" in by_id["G5.1_NUMERIC_OUTCOMES_MATCH"].flags
    assert by_id["G5.2_FIGURES_REGENERATE"].status == GateStatus.PASS


def test_gpu_blocked_paper_propagates_blocked_hardware_through_stage5(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "samples.csv").write_text("x,y\n1,1\n2,0\n3,1\n4,0\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("torch==2.3.0\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Repo\nRun `python main.py`.\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "import random\nrandom.seed(42)\nmodel = net.cuda()\n",
        encoding="utf-8",
    )

    called = False

    def should_not_run(
        args: list[str], *, cwd: Path | None = None, timeout: int = 300
    ) -> CommandResult:
        nonlocal called
        called = True
        return CommandResult(returncode=1, command=args)

    paper = _complete_ml_paper(code_files=["main.py"])
    sandbox = DockerSandboxRunner(config=SandboxConfig(has_gpu=False), runner=should_not_run)
    report, _ = evaluate_stage0_to_5_with_context(paper, workdir=tmp_path, sandbox=sandbox)

    assert called is False
    by_id = {res.gate_id: res for res in report.gate_results}
    assert by_id["G4.1_SANDBOX_FEASIBLE"].status == GateStatus.BLOCKED_HARDWARE
    assert by_id["G4.2_ENV_BUILDS"].status == GateStatus.BLOCKED_HARDWARE
    assert by_id["G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH"].status == GateStatus.BLOCKED_HARDWARE
    assert by_id["G5.1_NUMERIC_OUTCOMES_MATCH"].status == GateStatus.BLOCKED_HARDWARE
    assert by_id["G5.3_HUMAN_DOSSIER_READY"].status == GateStatus.PASS


def test_r_repository_commands_and_cli_dossier_output(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "survey.csv").write_text("x,y\n1,2\n3,4\n", encoding="utf-8")
    (tmp_path / "renv.lock").write_text("{}\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("Run `Rscript prep.R` then `Rscript main.R`.\n")
    (tmp_path / "prep.R").write_text("cat('prep done')\n", encoding="utf-8")
    (tmp_path / "main.R").write_text("set.seed(42)\n", encoding="utf-8")

    observed_cmds: list[list[str]] = []

    def fake_r_runner(
        args: list[str], *, cwd: Path | None = None, timeout: int = 300
    ) -> CommandResult:
        _ = cwd, timeout
        observed_cmds.append(args)
        if args[1] == "build":
            return CommandResult(returncode=0, stdout="built r image", command=args)
        if args[-1] == "main.R":
            (tmp_path / "results.json").write_text('{"r2": 0.74}', encoding="utf-8")
            (tmp_path / "fig.pdf").write_bytes(b"%PDF-1.4 mock")
        return CommandResult(returncode=0, stdout="ok", command=args)

    # Monkeypatch DockerSandboxRunner used inside CLI `--execute`
    monkeypatch.setattr(
        "researchability.gates.runner.DockerSandboxRunner",
        lambda cfg=None: DockerSandboxRunner(config=cfg, runner=fake_r_runner),
    )

    paper = PaperBundle(
        paper_id="doi:10.7910/DVN/RTEST",
        source_repo="dataverse",
        title="R Economics Replication",
        primary_profile=FieldProfile.ECONOMICS,
        abstract="Regression analysis yields R^2 = 0.74 and n = 150.",
        full_text="Regression yields R^2 = 0.74 with pre-analysis plan and placebo check.",
        sections={"Results": "R^2 = 0.74"},
        artifacts=ArtifactManifest(
            code_files=["prep.R", "main.R"],
            data_files=["data/survey.csv"],
            manifest_files=["renv.lock"],
            readme_files=["README.md"],
        ),
    )
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(paper.model_dump_json(indent=2), encoding="utf-8")
    out_json = tmp_path / "report.json"
    out_dossier = tmp_path / "dossier.md"

    cli_runner = CliRunner()
    res = cli_runner.invoke(
        app,
        [
            "audit",
            str(bundle_file),
            "--workdir",
            str(tmp_path),
            "--execute",
            "--output",
            str(out_json),
            "--dossier",
            str(out_dossier),
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["zero_touch_reproducible"] is True
    assert any(cmd[-2:] == ["Rscript", "prep.R"] for cmd in observed_cmds)
    assert any(cmd[-2:] == ["Rscript", "main.R"] for cmd in observed_cmds)
    assert "Human Review & Debate Dossier" in out_dossier.read_text(encoding="utf-8")
