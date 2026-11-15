"""Hardened Docker sandbox execution harness for untrusted research code."""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from researchability.models import EvidenceItem, GateStatus, PaperBundle

_GPU_HARD_REQUIREMENT_RE = re.compile(
    r"(?:\.cuda\s*\(|torch\.device\s*\(\s*['\"]cuda|"
    r"device\s*=\s*['\"]cuda['\"]|"
    r"tensorflow-gpu|nvidia-smi|--gpus\s+all|"
    r"\b(?:cupy|flash_attn|flash-attn|deepspeed|bitsandbytes|vllm|xformers)\b|"
    r"\b\d+\s*x\s*(?:a100|h100|v100)\b)",
    re.IGNORECASE,
)
_GPU_FALLBACK_RE = re.compile(
    r"(?:device\s*=\s*)?['\"]cuda(?::\d+)?['\"]\s+if\s+torch\.cuda\.is_available\s*\(\s*\)\s*"
    r"else\s+['\"]cpu['\"]",
    re.IGNORECASE,
)
_IGNORED_SCAN_DIRS = {".git", ".venv", "venv", "__pycache__", ".researchability_sandbox"}
_OUTPUT_TEXT_EXTS = {".json", ".csv", ".tsv", ".txt", ".log", ".md"}


class SandboxConfig(BaseModel):
    """Resource, isolation, and base-image limits for sandboxed runs."""

    cpu_limit: float = Field(default=2.0, gt=0.0)
    memory_limit: str = "4g"
    timeout_seconds: int = Field(default=300, ge=1)
    max_data_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1)
    docker_bin: str = "docker"
    python_image: str = "python:3.11-slim"
    r_image: str = "rocker/r-ver:4.4.1"
    has_gpu: bool = False
    network_disabled: bool = True
    non_root_user: str = "65534:65534"


class CommandResult(BaseModel):
    """Low-level subprocess execution capture."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_seconds: float = 0.0
    command: list[str] = Field(default_factory=list)


class SubprocessRunner(Protocol):
    """Callable protocol abstracting OS subprocess invocation for deterministic testing."""

    def __call__(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: int = 300,
    ) -> CommandResult: ...


class PreflightResult(BaseModel):
    """Outcome of G4.1 resource and hardware feasibility check."""

    feasible: bool
    status: GateStatus
    reason: str
    detected_language: str = "unknown"
    total_data_bytes: int = 0
    requires_gpu: bool = False
    evidence: list[EvidenceItem] = Field(default_factory=list)


class SandboxBuildResult(BaseModel):
    """Result of building an isolated container image from paper manifests."""

    success: bool
    image_tag: str
    language: str
    dockerfile_used: str = ""
    command_result: CommandResult


class SandboxRunResult(BaseModel):
    """Result of running a script/command inside an offline container."""

    success: bool
    command: list[str]
    command_result: CommandResult
    created_files: list[str] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    output_previews: dict[str, str] = Field(default_factory=dict)


def default_subprocess_runner(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 300,
) -> CommandResult:
    """Execute a subprocess with hard timeout and bounded stdout/stderr capture."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout[:50_000],
            stderr=completed.stderr[:50_000],
            timed_out=False,
            duration_seconds=round(time.monotonic() - started, 3),
            command=list(args),
        )
    except subprocess.TimeoutExpired as exc:
        stdout_val = (
            exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr_val = (
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        return CommandResult(
            returncode=124,
            stdout=stdout_val[:50_000],
            stderr=f"Command timed out after {timeout}s.\n{stderr_val}"[:50_000],
            timed_out=True,
            duration_seconds=round(time.monotonic() - started, 3),
            command=list(args),
        )
    except FileNotFoundError as exc:
        return CommandResult(
            returncode=127,
            stdout="",
            stderr=f"Executable not found: {exc.filename or args[0]}",
            timed_out=False,
            duration_seconds=round(time.monotonic() - started, 3),
            command=list(args),
        )


def _iter_workspace_files(root: Path | None) -> list[Path]:
    """Enumerate files under workdir skipping git/venv/sandbox metadata dirs."""
    if root is None or not root.exists():
        return []
    files: list[Path] = []
    for path in root.rglob("*"):
        if any(part in _IGNORED_SCAN_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            files.append(path)
    return files


def detect_primary_language(paper: PaperBundle, workdir: Path | None = None) -> str:
    """Infer primary runtime language ('python', 'r', 'mixed', or 'unknown')."""
    py_markers = 0
    r_markers = 0

    workdir_paths = (
        [str(p.relative_to(workdir)) for p in _iter_workspace_files(workdir)]
        if workdir is not None
        else []
    )
    all_paths = (
        list(paper.artifacts.code_files) + list(paper.artifacts.manifest_files) + workdir_paths
    )
    for rel in all_paths:
        lower = rel.lower()
        name = lower.rsplit("/", maxsplit=1)[-1]
        if lower.endswith((".py", ".ipynb")) or name in {
            "requirements.txt",
            "pyproject.toml",
            "uv.lock",
            "poetry.lock",
            "environment.yml",
            "setup.py",
        }:
            py_markers += 1
        elif lower.endswith((".r", ".rmd")) or name in {"renv.lock", "description"}:
            r_markers += 1

    if py_markers > 0 and r_markers > 0:
        return "mixed"
    if py_markers > 0:
        return "python"
    if r_markers > 0:
        return "r"
    return "unknown"


def _compute_total_data_bytes(paper: PaperBundle, workdir: Path | None) -> int:
    """Calculate known dataset footprint from workspace data files and artifact metadata."""
    total = 0
    seen_paths: set[Path] = set()

    if workdir is not None and workdir.exists():
        data_rel_set = {rel.lower() for rel in paper.artifacts.data_files}
        for file_path in _iter_workspace_files(workdir):
            rel_str = str(file_path.relative_to(workdir)).lower()
            is_data = (
                rel_str in data_rel_set
                or file_path.parts[len(workdir.parts) :].count("dataverse_files") > 0
                or file_path.suffix.lower()
                in {
                    ".csv",
                    ".tsv",
                    ".parquet",
                    ".feather",
                    ".h5",
                    ".hdf5",
                    ".rds",
                    ".rdata",
                    ".zip",
                }
            )
            if is_data and file_path not in seen_paths:
                seen_paths.add(file_path)
                try:
                    total += file_path.stat().st_size
                except OSError:
                    continue

    # Include remote metadata sizes when files are not yet materialized locally
    for link in paper.artifacts.links:
        if link.kind == "data" and link.size_bytes:
            if link.local_path and Path(link.local_path).exists():
                continue
            total += link.size_bytes

    return total


def check_sandbox_feasibility(
    paper: PaperBundle,
    workdir: Path | None = None,
    config: SandboxConfig | None = None,
) -> PreflightResult:
    """Evaluate G4.1 resource feasibility: data volume and GPU/accelerator constraints."""
    cfg = config or SandboxConfig()
    language = detect_primary_language(paper, workdir)
    total_bytes = _compute_total_data_bytes(paper, workdir)

    if total_bytes > cfg.max_data_bytes:
        limit_mb = cfg.max_data_bytes / (1024 * 1024)
        actual_mb = total_bytes / (1024 * 1024)
        return PreflightResult(
            feasible=False,
            status=GateStatus.BLOCKED_DATA_SIZE,
            reason=(
                f"Dataset footprint ({actual_mb:.1f} MiB) exceeds sandbox cap ({limit_mb:.1f} MiB)."
            ),
            detected_language=language,
            total_data_bytes=total_bytes,
            evidence=[
                EvidenceItem(
                    source="workspace_data",
                    excerpt=f"total_data_bytes={total_bytes}, max_data_bytes={cfg.max_data_bytes}",
                )
            ],
        )

    if not cfg.has_gpu:
        candidate_files: list[Path] = []
        if workdir is not None and workdir.exists():
            for path in _iter_workspace_files(workdir):
                if path.suffix.lower() in {
                    ".py",
                    ".r",
                    ".sh",
                    ".txt",
                    ".toml",
                    ".yml",
                    ".yaml",
                    ".md",
                }:
                    candidate_files.append(path)

        for path in candidate_files:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")[:50_000]
            except OSError:
                continue
            cleaned = _GPU_FALLBACK_RE.sub("", content)
            match = _GPU_HARD_REQUIREMENT_RE.search(cleaned)
            if match:
                rel_name = str(path.relative_to(workdir)) if workdir else str(path)
                return PreflightResult(
                    feasible=False,
                    status=GateStatus.BLOCKED_HARDWARE,
                    reason=(
                        "Code or manifest requires GPU/accelerator unavailable in CPU sandbox "
                        f"({match.group(0)})."
                    ),
                    detected_language=language,
                    total_data_bytes=total_bytes,
                    requires_gpu=True,
                    evidence=[
                        EvidenceItem(
                            source=rel_name,
                            excerpt=match.group(0),
                        )
                    ],
                )

    return PreflightResult(
        feasible=True,
        status=GateStatus.PASS,
        reason=(
            f"Sandbox execution feasible ({language}; data_bytes={total_bytes}, "
            f"cpu={cfg.cpu_limit}, mem={cfg.memory_limit})."
        ),
        detected_language=language,
        total_data_bytes=total_bytes,
        requires_gpu=False,
        evidence=[
            EvidenceItem(
                source="preflight",
                excerpt=f"language={language}, total_data_bytes={total_bytes}",
            )
        ],
    )


def _snapshot_files(workdir: Path) -> dict[str, tuple[int, int]]:
    """Record {relative_path: (size_bytes, mtime_ns)} before container execution."""
    snap: dict[str, tuple[int, int]] = {}
    for file_path in _iter_workspace_files(workdir):
        try:
            st = file_path.stat()
            snap[str(file_path.relative_to(workdir))] = (st.st_size, st.st_mtime_ns)
        except OSError:
            continue
    return snap


class DockerSandboxRunner:
    """Isolated Docker container builder and offline executor."""

    def __init__(
        self,
        config: SandboxConfig | None = None,
        runner: SubprocessRunner | None = None,
    ) -> None:
        self.config = config or SandboxConfig()
        self._run = runner or default_subprocess_runner

    def image_tag_for(self, paper: PaperBundle) -> str:
        """Compute deterministic lowercase Docker image tag for a PaperBundle."""
        digest = hashlib.sha256(paper.paper_id.encode("utf-8")).hexdigest()[:12]
        return f"researchability-sandbox:{digest}"

    def _find_manifest(self, workdir: Path, filename: str) -> Path | None:
        direct = workdir / filename
        if direct.is_file():
            return direct
        for candidate in _iter_workspace_files(workdir):
            if candidate.name == filename:
                return candidate
        return None

    def render_dockerfile(self, paper: PaperBundle, workdir: Path) -> str:
        """Generate a Dockerfile matching Python/R manifests when repo lacks one."""
        language = detect_primary_language(paper, workdir)
        req_file = self._find_manifest(workdir, "requirements.txt")
        pyproject_file = self._find_manifest(workdir, "pyproject.toml")
        renv_file = self._find_manifest(workdir, "renv.lock")

        if language == "r" or renv_file is not None:
            lines = [
                f"FROM {self.config.r_image}",
                "WORKDIR /workspace",
                "COPY . /workspace",
            ]
            if renv_file is not None:
                rel_renv = renv_file.relative_to(workdir)
                lines.append(
                    "RUN Rscript -e \"install.packages('renv', "
                    f"repos='https://cloud.r-project.org'); "
                    f"renv::restore(lockfile='{rel_renv}', prompt=FALSE)\""
                )
            lines.append("USER 65534:65534")
            return "\n".join(lines) + "\n"

        lines = [
            f"FROM {self.config.python_image}",
            "WORKDIR /workspace",
            "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1",
            "COPY . /workspace",
        ]
        if req_file is not None:
            rel_req = req_file.relative_to(workdir)
            lines.append(f"RUN pip install --no-cache-dir -r {rel_req}")
        elif pyproject_file is not None:
            lines.append("RUN pip install --no-cache-dir .")
        lines.append("USER 65534:65534")
        return "\n".join(lines) + "\n"

    def build_environment(self, paper: PaperBundle, workdir: Path) -> SandboxBuildResult:
        """Build container environment from existing Dockerfile or generated manifest image."""
        tag = self.image_tag_for(paper)
        language = detect_primary_language(paper, workdir)
        existing_dockerfile = self._find_manifest(workdir, "Dockerfile")

        if existing_dockerfile is not None:
            dockerfile_rel = str(existing_dockerfile.relative_to(workdir))
            cmd = [
                self.config.docker_bin,
                "build",
                "-t",
                tag,
                "-f",
                str(existing_dockerfile),
                str(workdir),
            ]
            result = self._run(cmd, cwd=workdir, timeout=self.config.timeout_seconds)
            return SandboxBuildResult(
                success=result.returncode == 0 and not result.timed_out,
                image_tag=tag,
                language=language,
                dockerfile_used=dockerfile_rel,
                command_result=result,
            )

        helper_dir = workdir / ".researchability_sandbox"
        helper_dir.mkdir(parents=True, exist_ok=True)
        generated_df = helper_dir / "Dockerfile.generated"
        dockerfile_text = self.render_dockerfile(paper, workdir)
        generated_df.write_text(dockerfile_text, encoding="utf-8")

        cmd = [
            self.config.docker_bin,
            "build",
            "-t",
            tag,
            "-f",
            str(generated_df),
            str(workdir),
        ]
        result = self._run(cmd, cwd=workdir, timeout=self.config.timeout_seconds)
        return SandboxBuildResult(
            success=result.returncode == 0 and not result.timed_out,
            image_tag=tag,
            language=language,
            dockerfile_used=".researchability_sandbox/Dockerfile.generated",
            command_result=result,
        )

    def build_run_args(
        self,
        image_tag: str,
        command: list[str],
        workdir: Path,
        *,
        data_mount: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Construct hardened `docker run` arguments enforcing isolation limits."""
        args = [
            self.config.docker_bin,
            "run",
            "--rm",
            f"--cpus={self.config.cpu_limit}",
            f"--memory={self.config.memory_limit}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            f"--user={self.config.non_root_user}",
        ]
        if self.config.network_disabled:
            args.append("--network=none")

        args.extend(["-v", f"{workdir.resolve()}:/workspace"])
        if data_mount is not None and data_mount.exists():
            args.extend(["-v", f"{data_mount.resolve()}:/data:ro"])

        if env:
            for key, value in sorted(env.items()):
                args.extend(["-e", f"{key}={value}"])

        args.extend(["-w", "/workspace", image_tag, *command])
        return args

    def run_command(
        self,
        image_tag: str,
        command: list[str],
        workdir: Path,
        *,
        data_mount: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxRunResult:
        """Execute a command inside the isolated container and diff generated artifacts."""
        before = _snapshot_files(workdir)
        docker_args = self.build_run_args(
            image_tag=image_tag,
            command=command,
            workdir=workdir,
            data_mount=data_mount,
            env=env,
        )
        cmd_res = self._run(docker_args, cwd=workdir, timeout=self.config.timeout_seconds)
        after = _snapshot_files(workdir)

        created = sorted(rel for rel in after if rel not in before)
        modified = sorted(rel for rel in after if rel in before and after[rel] != before[rel])

        previews: dict[str, str] = {}
        for rel in [*created, *modified]:
            file_path = workdir / rel
            if file_path.suffix.lower() in _OUTPUT_TEXT_EXTS and file_path.is_file():
                try:
                    previews[rel] = file_path.read_text(encoding="utf-8", errors="replace")[:8_000]
                except OSError:
                    continue

        return SandboxRunResult(
            success=cmd_res.returncode == 0 and not cmd_res.timed_out,
            command=list(command),
            command_result=cmd_res,
            created_files=created,
            modified_files=modified,
            output_previews=previews,
        )
