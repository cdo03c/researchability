"""Unit tests for the hardened Docker sandbox runner and resource preflight."""

from __future__ import annotations

import sys
from pathlib import Path

from researchability.models import (
    ArtifactLink,
    ArtifactManifest,
    FieldProfile,
    GateStatus,
    PaperBundle,
)
from researchability.sandbox import (
    CommandResult,
    DockerSandboxRunner,
    SandboxConfig,
    check_sandbox_feasibility,
    default_subprocess_runner,
)


def _paper(
    *,
    paper_id: str = "arxiv:2401.10001",
    code_files: list[str] | None = None,
    data_files: list[str] | None = None,
    manifest_files: list[str] | None = None,
    links: list[ArtifactLink] | None = None,
) -> PaperBundle:
    return PaperBundle(
        paper_id=paper_id,
        source_repo="arxiv",
        title="Sandbox Test Study",
        primary_profile=FieldProfile.ML_CS,
        artifacts=ArtifactManifest(
            links=links or [],
            code_files=code_files or ["main.py"],
            data_files=data_files or ["data.csv"],
            manifest_files=manifest_files or ["requirements.txt"],
            readme_files=["README.md"],
        ),
    )


def test_preflight_passes_for_small_cpu_python_repo(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text(
        'device = "cuda" if torch.cuda.is_available() else "cpu"\nprint("ok")\n',
        encoding="utf-8",
    )
    (tmp_path / "data.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("numpy==1.26.4\n", encoding="utf-8")

    preflight = check_sandbox_feasibility(_paper(), tmp_path, SandboxConfig())
    assert preflight.feasible is True
    assert preflight.status == GateStatus.PASS
    assert preflight.detected_language == "python"
    assert preflight.requires_gpu is False


def test_preflight_blocks_hard_gpu_requirement(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text(
        "import torch\nmodel = Net().cuda()\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("torch==2.3.0\n", encoding="utf-8")

    preflight = check_sandbox_feasibility(_paper(), tmp_path, SandboxConfig(has_gpu=False))
    assert preflight.feasible is False
    assert preflight.status == GateStatus.BLOCKED_HARDWARE
    assert preflight.requires_gpu is True

    gpu_preflight = check_sandbox_feasibility(_paper(), tmp_path, SandboxConfig(has_gpu=True))
    assert gpu_preflight.feasible is True
    assert gpu_preflight.status == GateStatus.PASS


def test_preflight_blocks_oversized_local_and_remote_datasets(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "big.csv").write_bytes(b"0" * 2048)

    local_blocked = check_sandbox_feasibility(
        _paper(data_files=["big.csv"]),
        tmp_path,
        SandboxConfig(max_data_bytes=1024),
    )
    assert local_blocked.status == GateStatus.BLOCKED_DATA_SIZE

    remote_paper = _paper(
        data_files=[],
        links=[
            ArtifactLink(
                url="https://example.org/huge.parquet",
                kind="data",
                size_bytes=5_000_000,
            )
        ],
    )
    remote_blocked = check_sandbox_feasibility(
        remote_paper,
        None,
        SandboxConfig(max_data_bytes=1_000_000),
    )
    assert remote_blocked.status == GateStatus.BLOCKED_DATA_SIZE


def test_render_dockerfile_supports_python_and_r(tmp_path: Path) -> None:
    runner = DockerSandboxRunner()
    (tmp_path / "requirements.txt").write_text("scipy==1.13.0\n", encoding="utf-8")
    py_df = runner.render_dockerfile(_paper(), tmp_path)
    assert "FROM python:3.11-slim" in py_df
    assert "pip install --no-cache-dir -r requirements.txt" in py_df
    assert "USER 65534:65534" in py_df

    r_dir = tmp_path / "r_proj"
    r_dir.mkdir()
    (r_dir / "main.R").write_text("cat('hello\\n')\n", encoding="utf-8")
    (r_dir / "renv.lock").write_text("{}\n", encoding="utf-8")
    r_paper = _paper(code_files=["main.R"], manifest_files=["renv.lock"])
    r_df = runner.render_dockerfile(r_paper, r_dir)
    assert "FROM rocker/r-ver:4.4.1" in r_df
    assert "renv::restore" in r_df


def test_build_and_run_enforce_isolation_flags_and_diff_files(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('run')\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("numpy==1.26.4\n", encoding="utf-8")
    data_dir = tmp_path / "dataverse_files"
    data_dir.mkdir()
    (data_dir / "input.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    recorded_cmds: list[list[str]] = []

    def fake_subproc(
        args: list[str], *, cwd: Path | None = None, timeout: int = 300
    ) -> CommandResult:
        _ = cwd, timeout
        recorded_cmds.append(args)
        if args[1] == "build":
            return CommandResult(returncode=0, stdout="Successfully built mockimg", command=args)
        # Simulate container creating an output JSON and PNG in mounted workspace
        (tmp_path / "metrics.json").write_text('{"accuracy": 0.91}', encoding="utf-8")
        (tmp_path / "fig1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        return CommandResult(returncode=0, stdout="accuracy = 0.91\n", command=args)

    runner = DockerSandboxRunner(
        config=SandboxConfig(cpu_limit=1.5, memory_limit="2g", timeout_seconds=45),
        runner=fake_subproc,
    )
    paper = _paper()
    build_res = runner.build_environment(paper, tmp_path)
    assert build_res.success is True
    assert build_res.dockerfile_used == ".researchability_sandbox/Dockerfile.generated"
    assert (tmp_path / ".researchability_sandbox" / "Dockerfile.generated").exists()

    run_res = runner.run_command(
        build_res.image_tag,
        ["python", "main.py"],
        tmp_path,
        data_mount=data_dir,
    )
    assert run_res.success is True
    assert "metrics.json" in run_res.created_files
    assert "fig1.png" in run_res.created_files
    assert "metrics.json" in run_res.output_previews

    docker_run_args = recorded_cmds[1]
    assert "--network=none" in docker_run_args
    assert "--cpus=1.5" in docker_run_args
    assert "--memory=2g" in docker_run_args
    assert "--user=65534:65534" in docker_run_args
    assert "--cap-drop=ALL" in docker_run_args
    assert f"{data_dir.resolve()}:/data:ro" in docker_run_args


def test_default_subprocess_runner_handles_missing_and_timeout(tmp_path: Path) -> None:
    missing = default_subprocess_runner(["__nonexistent_binary_xyz__"])
    assert missing.returncode == 127
    assert "Executable not found" in missing.stderr

    ok = default_subprocess_runner([sys.executable, "-c", "print('hello')"], cwd=tmp_path)
    assert ok.returncode == 0
    assert "hello" in ok.stdout

    timed_out = default_subprocess_runner(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        timeout=1,
    )
    assert timed_out.timed_out is True
    assert timed_out.returncode == 124
