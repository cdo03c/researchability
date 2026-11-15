"""Stage 4 evaluators: resource feasibility, container build, zero-touch & repaired runs."""

from __future__ import annotations

import difflib
import re
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from researchability.gates.base import EvaluationContext
from researchability.llm.client import ModelTier
from researchability.models import EvidenceItem, GateResult, GateStatus
from researchability.sandbox import (
    DockerSandboxRunner,
    SandboxRunResult,
    check_sandbox_feasibility,
)

_README_CMD_RE = re.compile(
    r"(?:^|\s|`)(python3?|Rscript|bash|sh)\s+([^\s`\"';|&]+\.(?:py|R|r|sh))\b",
    re.MULTILINE,
)
_PREP_NAME_RE = re.compile(r"(?:^|[/_-])(prep|preprocess|clean|etl|prepare)", re.IGNORECASE)
_MAIN_PRIORITY_STEMS = (
    "main",
    "run",
    "analysis",
    "reproduce",
    "evaluate",
    "eval",
    "train",
    "experiment",
    "pipeline",
    "tables",
    "figures",
)
_HARDCODED_ABS_RE = re.compile(r"""(['"])((?:/Users/|/home/|/tmp/|/var/|[A-Za-z]:[/\\])[^'"]+)\1""")
_MISSING_FILE_RE = re.compile(r"""No such file or directory:\s*['"]([^'"]+)['"]""")
_MISSING_MODULE_RE = re.compile(r"""No module named\s+['"]([^'"]+)['"]""")
_IGNORED_COPY_DIRS = {".git", ".venv", "venv", "__pycache__", ".researchability_sandbox"}
_MAX_REPAIR_CHANGED_LINES = 20


class _LLMRepairPatch(BaseModel):
    """Structured minimal patch proposed by Gemini for minor mechanical execution errors."""

    relative_file: str
    original_snippet: str
    replacement_snippet: str
    explanation: str = Field(default="Minor mechanical path/import fix")


def _get_sandbox(ctx: EvaluationContext) -> DockerSandboxRunner:
    existing = ctx.extracted_claims.get("sandbox_runner")
    if isinstance(existing, DockerSandboxRunner):
        return existing
    runner = DockerSandboxRunner()
    ctx.extracted_claims["sandbox_runner"] = runner
    return runner


def _command_for_script(rel_path: str) -> list[str]:
    lower = rel_path.lower()
    if lower.endswith(".r"):
        return ["Rscript", rel_path]
    if lower.endswith(".sh"):
        return ["bash", rel_path]
    return ["python", rel_path]


def _resolve_script_relpath(workdir: Path, candidate: str) -> str | None:
    direct = workdir / candidate
    if direct.is_file():
        return candidate
    name = Path(candidate).name
    for path in sorted(workdir.rglob(name)):
        rel = path.relative_to(workdir)
        if any(part in _IGNORED_COPY_DIRS for part in rel.parts):
            continue
        if path.is_file():
            return rel.as_posix()
    return None


def _list_workspace_scripts(workdir: Path) -> list[str]:
    scripts: list[str] = []
    for path in sorted(workdir.rglob("*")):
        rel_parts = path.relative_to(workdir).parts
        if any(part in _IGNORED_COPY_DIRS for part in rel_parts):
            continue
        if path.is_file() and path.suffix.lower() in {".py", ".r", ".sh"}:
            scripts.append(path.relative_to(workdir).as_posix())
    return scripts


def _discover_prep_commands(ctx: EvaluationContext, workdir: Path) -> list[list[str]]:
    candidates = list(ctx.paper.artifacts.code_files) + _list_workspace_scripts(workdir)
    seen: set[str] = set()
    commands: list[list[str]] = []
    for raw in candidates:
        if not _PREP_NAME_RE.search(raw):
            continue
        resolved = _resolve_script_relpath(workdir, raw)
        if resolved and resolved not in seen:
            seen.add(resolved)
            commands.append(_command_for_script(resolved))
    return commands


def _discover_main_command(ctx: EvaluationContext, workdir: Path) -> list[str] | None:
    # 1. Check README explicitly documented commands first
    for readme_path, content in ctx.read_text_files(ctx.paper.artifacts.readme_files).items():
        _ = readme_path
        for match in _README_CMD_RE.finditer(content):
            tool, script_raw = match.group(1), match.group(2)
            if _PREP_NAME_RE.search(script_raw):
                continue
            resolved = _resolve_script_relpath(workdir, script_raw)
            if resolved:
                return [tool if tool != "python3" else "python", resolved]

    # 2. Match conventional entrypoint names in code_files / workspace
    pool: list[str] = []
    seen: set[str] = set()
    for raw in [*ctx.paper.artifacts.code_files, *_list_workspace_scripts(workdir)]:
        resolved = _resolve_script_relpath(workdir, raw)
        if resolved and resolved not in seen and not _PREP_NAME_RE.search(resolved):
            seen.add(resolved)
            pool.append(resolved)

    for stem in _MAIN_PRIORITY_STEMS:
        for rel in pool:
            if Path(rel).stem.lower() == stem:
                return _command_for_script(rel)

    # 3. Single non-test non-prep script fallback
    non_test = [rel for rel in pool if "test" not in Path(rel).stem.lower()]
    if non_test:
        return _command_for_script(non_test[0])
    if pool:
        return _command_for_script(pool[0])
    return None


def _detect_data_mount(workdir: Path) -> Path | None:
    dv_dir = workdir / "dataverse_files"
    if dv_dir.is_dir():
        return dv_dir
    data_dir = workdir / "data"
    if data_dir.is_dir():
        return data_dir
    return None


def _copy_workspace_for_repair(workdir: Path) -> Path:
    repair_dir = workdir / ".researchability_sandbox" / "repaired_workspace"
    if repair_dir.exists():
        shutil.rmtree(repair_dir)
    repair_dir.mkdir(parents=True, exist_ok=True)
    for item in workdir.iterdir():
        if item.name in _IGNORED_COPY_DIRS:
            continue
        dest = repair_dir / item.name
        if item.is_dir():
            shutil.copytree(
                item,
                dest,
                ignore=shutil.ignore_patterns(*sorted(_IGNORED_COPY_DIRS)),
            )
        else:
            shutil.copy2(item, dest)
    return repair_dir


def _find_workspace_file_by_name(root: Path, target_name: str) -> str | None:
    for path in sorted(root.rglob(target_name)):
        rel = path.relative_to(root)
        if any(part in _IGNORED_COPY_DIRS for part in rel.parts):
            continue
        if path.is_file():
            return rel.as_posix()
    return None


def _compute_unified_diff(rel_path: str, old_text: str, new_text: str) -> str:
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
    )


def _apply_deterministic_repairs(
    repair_dir: Path,
    script_rel: str,
    stderr: str,
) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
    """Apply bounded deterministic fixes; return ([(file, reason, diff)], env_overrides)."""
    diffs: list[tuple[str, str, str]] = []
    env_overrides: dict[str, str] = {}

    script_path = repair_dir / script_rel
    if not script_path.is_file():
        return diffs, env_overrides

    original = script_path.read_text(encoding="utf-8", errors="replace")
    updated = original
    reasons: list[str] = []

    # 1. Replace hardcoded absolute file paths pointing to workspace filenames
    def _replace_abs(match: re.Match[str]) -> str:
        quote, raw_path = match.group(1), match.group(2)
        filename = Path(raw_path.replace("\\", "/")).name
        found_rel = _find_workspace_file_by_name(repair_dir, filename)
        if found_rel:
            reasons.append(f"Rewrote hardcoded path '{raw_path}' -> '{found_rel}'.")
            return f"{quote}{found_rel}{quote}"
        return match.group(0)

    updated = _HARDCODED_ABS_RE.sub(_replace_abs, updated)

    # 2. Resolve missing relative input/output paths cited in stderr
    repair_root = repair_dir.resolve()
    for missing_match in _MISSING_FILE_RE.finditer(stderr):
        missing_str = missing_match.group(1)
        missing_name = Path(missing_str.replace("\\", "/")).name
        found_rel = _find_workspace_file_by_name(repair_dir, missing_name)
        if found_rel and found_rel != missing_str and missing_str in updated:
            updated = updated.replace(missing_str, found_rel)
            reasons.append(f"Updated relative path '{missing_str}' -> '{found_rel}'.")
        elif (
            not Path(missing_str).is_absolute()
            and ".." not in Path(missing_str).parts
            and ("/" in missing_str or "\\" in missing_str)
        ):
            parent_dir = (repair_dir / missing_str).parent.resolve()
            if str(parent_dir).startswith(str(repair_root)) and not parent_dir.exists():
                parent_dir.mkdir(parents=True, exist_ok=True)
                if script_path.suffix.lower() == ".py" and "makedirs" not in updated:
                    rel_parent = Path(missing_str).parent.as_posix()
                    inject = f"import os\nos.makedirs({rel_parent!r}, exist_ok=True)\n"
                    updated = inject + updated
                reasons.append(f"Created missing output directory for '{missing_str}'.")

    # 3. Handle missing local PYTHONPATH / module imports
    mod_match = _MISSING_MODULE_RE.search(stderr)
    if mod_match:
        mod_name = mod_match.group(1).split(".")[0]
        if (
            (repair_dir / f"{mod_name}.py").is_file()
            or (repair_dir / "src" / mod_name).exists()
            or (repair_dir / mod_name).is_dir()
        ):
            env_overrides["PYTHONPATH"] = "/workspace:/workspace/src"
            reasons.append(f"Set PYTHONPATH for local module '{mod_name}'.")

    if updated != original:
        script_path.write_text(updated, encoding="utf-8")
        diff_text = _compute_unified_diff(script_rel, original, updated)
        diffs.append((script_rel, "; ".join(reasons) or "Mechanical path fix", diff_text))

    return diffs, env_overrides


def _apply_llm_repair(
    ctx: EvaluationContext,
    repair_dir: Path,
    script_rel: str,
    stderr: str,
) -> tuple[str, str, str] | None:
    """Ask Gemini for a bounded single-file repair patch when deterministic fixes cannot help."""
    if ctx.llm is None:
        return None
    script_path = repair_dir / script_rel
    if not script_path.is_file():
        return None

    old_text = script_path.read_text(encoding="utf-8", errors="replace")
    workspace_listing = "\n".join(_list_workspace_scripts(repair_dir)[:30])
    prompt = (
        "Suggest ONE minimal mechanical fix (path, directory creation, or local import) so this "
        "research script runs cleanly in `/workspace`. "
        "Do NOT alter statistical logic or constants.\n\n"
        f"Target script: {script_rel}\n"
        f"Stderr:\n{stderr[:3000]}\n\n"
        f"Workspace scripts:\n{workspace_listing}\n\n"
        f"Script contents:\n{old_text[:8000]}"
    )
    patch, _ = ctx.llm.generate_structured(
        paper_id=ctx.paper.paper_id,
        gate_id="G4.5_ANALYSIS_EXECUTES_REPAIRED",
        prompt=prompt,
        schema_cls=_LLMRepairPatch,
        tier=ModelTier.FAST,
    )
    target_path = (repair_dir / patch.relative_file).resolve()
    if not str(target_path).startswith(str(repair_dir.resolve())) or not target_path.is_file():
        return None

    file_before = target_path.read_text(encoding="utf-8", errors="replace")
    if patch.original_snippet not in file_before:
        return None
    file_after = file_before.replace(patch.original_snippet, patch.replacement_snippet, 1)
    diff_text = _compute_unified_diff(patch.relative_file, file_before, file_after)
    changed_lines = [
        line
        for line in diff_text.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    if len(changed_lines) > _MAX_REPAIR_CHANGED_LINES or not changed_lines:
        return None

    target_path.write_text(file_after, encoding="utf-8")
    return patch.relative_file, patch.explanation, diff_text


def eval_g4_1_sandbox_feasible(ctx: EvaluationContext) -> GateResult:
    """Verify job fits sandbox CPU/RAM/disk/GPU limits cleanly (G4.1)."""
    sandbox = _get_sandbox(ctx)
    preflight = check_sandbox_feasibility(ctx.paper, ctx.workdir, sandbox.config)
    ctx.extracted_claims["sandbox_preflight"] = preflight
    flags: list[str] = []
    if preflight.status == GateStatus.BLOCKED_HARDWARE:
        flags.append("HARDWARE_GPU_REQUIRED")
    elif preflight.status == GateStatus.BLOCKED_DATA_SIZE:
        flags.append("OVERSIZED_DATASET")

    return GateResult(
        gate_id="G4.1_SANDBOX_FEASIBLE",
        status=preflight.status,
        rationale=preflight.reason,
        evidence=preflight.evidence,
        flags=flags,
    )


def eval_g4_2_env_builds(ctx: EvaluationContext) -> GateResult:
    """Build container environment from repository manifests inside sandbox (G4.2)."""
    if ctx.workdir is None or not ctx.workdir.exists():
        return GateResult(
            gate_id="G4.2_ENV_BUILDS",
            status=GateStatus.FAIL,
            rationale="Cannot build container environment: no local workspace directory provided.",
        )

    sandbox = _get_sandbox(ctx)
    build_res = sandbox.build_environment(ctx.paper, ctx.workdir)
    ctx.extracted_claims["sandbox_image_tag"] = build_res.image_tag
    ctx.extracted_claims["sandbox_build_result"] = build_res

    excerpt = (
        build_res.command_result.stderr or build_res.command_result.stdout or build_res.image_tag
    )
    if build_res.success:
        return GateResult(
            gate_id="G4.2_ENV_BUILDS",
            status=GateStatus.PASS,
            rationale=(
                f"Container image '{build_res.image_tag}' built cleanly "
                f"({build_res.language}, Dockerfile={build_res.dockerfile_used})."
            ),
            evidence=[
                EvidenceItem(
                    source=build_res.dockerfile_used or "Dockerfile",
                    excerpt=excerpt[:400],
                )
            ],
        )

    return GateResult(
        gate_id="G4.2_ENV_BUILDS",
        status=GateStatus.FAIL,
        rationale=f"Container build failed (exit={build_res.command_result.returncode}).",
        evidence=[
            EvidenceItem(
                source=build_res.dockerfile_used or "docker build",
                excerpt=excerpt[:600],
            )
        ],
    )


def eval_g4_3_prep_executes(ctx: EvaluationContext) -> GateResult:
    """Execute documented data preparation scripts inside offline sandbox (G4.3)."""
    if ctx.workdir is None or not ctx.workdir.exists():
        return GateResult(
            gate_id="G4.3_PREP_EXECUTES",
            status=GateStatus.FAIL,
            rationale="Cannot run data preparation without local workspace.",
        )

    sandbox = _get_sandbox(ctx)
    image_tag = str(
        ctx.extracted_claims.get("sandbox_image_tag") or sandbox.image_tag_for(ctx.paper)
    )
    prep_commands = _discover_prep_commands(ctx, ctx.workdir)
    if not prep_commands:
        return GateResult(
            gate_id="G4.3_PREP_EXECUTES",
            status=GateStatus.FAIL,
            rationale="No executable data-preparation script resolved in workspace.",
        )

    data_mount = _detect_data_mount(ctx.workdir)
    prep_runs: list[SandboxRunResult] = []
    for cmd in prep_commands:
        run_res = sandbox.run_command(image_tag, cmd, ctx.workdir, data_mount=data_mount)
        prep_runs.append(run_res)
        if not run_res.success:
            excerpt = run_res.command_result.stderr or run_res.command_result.stdout
            return GateResult(
                gate_id="G4.3_PREP_EXECUTES",
                status=GateStatus.FAIL,
                rationale=(
                    f"Data-prep command '{' '.join(cmd)}' failed "
                    f"(exit={run_res.command_result.returncode})."
                ),
                evidence=[EvidenceItem(source=" ".join(cmd), excerpt=excerpt[:600])],
            )

    ctx.extracted_claims["prep_runs"] = prep_runs
    created_summary = sorted({f for run in prep_runs for f in run.created_files})
    return GateResult(
        gate_id="G4.3_PREP_EXECUTES",
        status=GateStatus.PASS,
        rationale=(
            f"Executed {len(prep_runs)} data-preparation command(s) cleanly in offline sandbox."
        ),
        evidence=[
            EvidenceItem(
                source=" ".join(prep_runs[0].command),
                excerpt=f"created_files={created_summary[:10]}",
            )
        ],
    )


def eval_g4_4_analysis_zero_touch(ctx: EvaluationContext) -> GateResult:
    """Run main analysis unmodified inside offline sandbox (G4.4)."""
    if ctx.workdir is None or not ctx.workdir.exists():
        return GateResult(
            gate_id="G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH",
            status=GateStatus.FAIL,
            rationale="Cannot run analysis without local workspace.",
        )

    sandbox = _get_sandbox(ctx)
    image_tag = str(
        ctx.extracted_claims.get("sandbox_image_tag") or sandbox.image_tag_for(ctx.paper)
    )
    main_cmd = _discover_main_command(ctx, ctx.workdir)
    if main_cmd is None:
        return GateResult(
            gate_id="G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH",
            status=GateStatus.FAIL,
            rationale="Could not identify an executable main analysis entrypoint.",
        )

    ctx.extracted_claims["main_command"] = main_cmd
    data_mount = _detect_data_mount(ctx.workdir)
    run_res = sandbox.run_command(image_tag, main_cmd, ctx.workdir, data_mount=data_mount)
    ctx.extracted_claims["analysis_run_zero_touch"] = run_res

    if run_res.success:
        ctx.extracted_claims["analysis_run_effective"] = run_res
        ctx.extracted_claims["analysis_workdir"] = ctx.workdir
        return GateResult(
            gate_id="G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH",
            status=GateStatus.PASS,
            rationale=(
                f"Main analysis '{' '.join(main_cmd)}' completed zero-touch in offline sandbox."
            ),
            evidence=[
                EvidenceItem(
                    source=" ".join(main_cmd),
                    excerpt=(
                        f"created={run_res.created_files[:10]}; "
                        f"stdout={run_res.command_result.stdout[:200]}"
                    ),
                )
            ],
        )

    excerpt = run_res.command_result.stderr or run_res.command_result.stdout or "Non-zero exit"
    return GateResult(
        gate_id="G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH",
        status=GateStatus.FAIL,
        rationale=(
            f"Zero-touch analysis '{' '.join(main_cmd)}' failed "
            f"(exit={run_res.command_result.returncode})."
        ),
        evidence=[EvidenceItem(source=" ".join(main_cmd), excerpt=excerpt[:600])],
        flags=["ZERO_TOUCH_EXECUTION_FAILED"],
    )


def eval_g4_5_analysis_repaired(ctx: EvaluationContext) -> GateResult:
    """Attempt bounded, diff-logged mechanical repair when zero-touch fails (G4.5)."""
    zero_touch: SandboxRunResult | None = ctx.extracted_claims.get("analysis_run_zero_touch")
    if zero_touch is not None and zero_touch.success:
        return GateResult(
            gate_id="G4.5_ANALYSIS_EXECUTES_REPAIRED",
            status=GateStatus.N_A,
            rationale="Not applicable: main analysis already passed zero-touch (G4.4).",
        )

    if ctx.workdir is None or not ctx.workdir.exists():
        return GateResult(
            gate_id="G4.5_ANALYSIS_EXECUTES_REPAIRED",
            status=GateStatus.FAIL,
            rationale="Cannot attempt repair without local workspace.",
        )

    main_cmd: list[str] | None = ctx.extracted_claims.get("main_command") or _discover_main_command(
        ctx, ctx.workdir
    )
    if main_cmd is None or len(main_cmd) < 2:
        return GateResult(
            gate_id="G4.5_ANALYSIS_EXECUTES_REPAIRED",
            status=GateStatus.FAIL,
            rationale="No target entrypoint script available to repair.",
        )

    sandbox = _get_sandbox(ctx)
    image_tag = str(
        ctx.extracted_claims.get("sandbox_image_tag") or sandbox.image_tag_for(ctx.paper)
    )
    repair_dir = _copy_workspace_for_repair(ctx.workdir)
    data_mount = _detect_data_mount(repair_dir)
    script_rel = main_cmd[1]
    current_stderr = (zero_touch.command_result.stderr if zero_touch is not None else "") or (
        zero_touch.command_result.stdout if zero_touch is not None else ""
    )

    applied_diffs: list[tuple[str, str, str]] = []
    env_overrides: dict[str, str] = {}
    repaired_run: SandboxRunResult | None = None

    for _attempt in range(2):
        step_diffs, step_env = _apply_deterministic_repairs(
            repair_dir=repair_dir,
            script_rel=script_rel,
            stderr=current_stderr,
        )
        if not step_diffs and not step_env and ctx.llm is not None:
            llm_patch = _apply_llm_repair(ctx, repair_dir, script_rel, current_stderr)
            if llm_patch is not None:
                step_diffs.append(llm_patch)

        if not step_diffs and not step_env:
            break

        applied_diffs.extend(step_diffs)
        env_overrides.update(step_env)
        repaired_run = sandbox.run_command(
            image_tag,
            main_cmd,
            repair_dir,
            data_mount=data_mount,
            env=env_overrides or None,
        )
        if repaired_run.success:
            break
        current_stderr = repaired_run.command_result.stderr or repaired_run.command_result.stdout

    if not applied_diffs and not env_overrides:
        return GateResult(
            gate_id="G4.5_ANALYSIS_EXECUTES_REPAIRED",
            status=GateStatus.FAIL,
            rationale="No safe mechanical path/import repair applicable to failed analysis.",
            flags=["REPAIR_NOT_APPLICABLE"],
        )

    assert repaired_run is not None
    ctx.extracted_claims["analysis_run_repaired"] = repaired_run
    ctx.extracted_claims["repair_diffs"] = [
        {"file": f, "reason": reason, "diff": diff} for f, reason, diff in applied_diffs
    ]

    evidence = [
        EvidenceItem(source=file_rel, excerpt=reason, note=diff_text[:2000])
        for file_rel, reason, diff_text in applied_diffs
    ]
    if not evidence and env_overrides:
        evidence.append(EvidenceItem(source="env", excerpt=f"env_overrides={env_overrides}"))

    if repaired_run.success:
        ctx.extracted_claims["analysis_run_effective"] = repaired_run
        ctx.extracted_claims["analysis_workdir"] = repair_dir
        return GateResult(
            gate_id="G4.5_ANALYSIS_EXECUTES_REPAIRED",
            status=GateStatus.PASS,
            rationale=(
                f"Main analysis '{' '.join(main_cmd)}' succeeded after logged minor repair "
                f"({len(applied_diffs)} file diff(s))."
            ),
            evidence=evidence,
            flags=["MINOR_REPAIR_APPLIED"],
        )

    err_excerpt = repaired_run.command_result.stderr or repaired_run.command_result.stdout
    return GateResult(
        gate_id="G4.5_ANALYSIS_EXECUTES_REPAIRED",
        status=GateStatus.FAIL,
        rationale=(
            f"Repaired analysis '{' '.join(main_cmd)}' still failed "
            f"(exit={repaired_run.command_result.returncode})."
        ),
        evidence=[*evidence, EvidenceItem(source=" ".join(main_cmd), excerpt=err_excerpt[:500])],
        flags=["REPAIR_FAILED"],
    )


STAGE4_EVALUATORS = {
    "G4.1_SANDBOX_FEASIBLE": eval_g4_1_sandbox_feasible,
    "G4.2_ENV_BUILDS": eval_g4_2_env_builds,
    "G4.3_PREP_EXECUTES": eval_g4_3_prep_executes,
    "G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH": eval_g4_4_analysis_zero_touch,
    "G4.5_ANALYSIS_EXECUTES_REPAIRED": eval_g4_5_analysis_repaired,
}
