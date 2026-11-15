"""Stage 4 evaluators: resource feasibility, container build, zero-touch & repaired runs."""

from __future__ import annotations

import ast
import difflib
import re
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from researchability.connectors.links import safe_resolve_under
from researchability.gates.base import EvaluationContext
from researchability.llm.client import GeminiApiError, MissingApiKeyError, ModelTier
from researchability.models import EvidenceItem, GateResult, GateStatus
from researchability.profiler.robustness import compare_distributions
from researchability.sandbox import (
    DockerSandboxRunner,
    SandboxRunResult,
    check_sandbox_feasibility,
)

_README_CMD_RE = re.compile(
    r"(?:^|\s|`)(python3?|Rscript|bash|sh|julia|stata(?:-mp|-se)?)\s+"
    r"(?:-\w+\s+|do\s+)?([^\s`\"';|&]+\.(?:py|R|r|sh|jl|do))\b",
    re.MULTILINE,
)
_README_NOTEBOOK_RE = re.compile(
    r"(?:jupyter\s+nbconvert[^\n`]*?|papermill)\s+([^\s`\"';|&]+\.ipynb)\b",
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
    if lower.endswith(".do"):
        return ["stata-mp", "-b", "do", rel_path]
    if lower.endswith(".jl"):
        return ["julia", rel_path]
    if lower.endswith(".ipynb"):
        return ["jupyter", "nbconvert", "--to", "notebook", "--execute", "--inplace", rel_path]
    return ["python", rel_path]


def _resolve_script_relpath(workdir: Path, candidate: str) -> str | None:
    cand_path = Path(candidate)
    if cand_path.is_absolute() or ".." in cand_path.parts:
        return None
    root_resolved = workdir.resolve()
    direct = safe_resolve_under(root_resolved, cand_path)
    if direct is not None and direct.is_file():
        return cand_path.as_posix()
    name = cand_path.name
    for path in sorted(workdir.rglob(name)):
        rel = path.relative_to(workdir)
        if any(part in _IGNORED_COPY_DIRS for part in rel.parts):
            continue
        if path.is_file() and path.resolve().is_relative_to(root_resolved):
            return rel.as_posix()
    return None


def _list_workspace_scripts(workdir: Path) -> list[str]:
    scripts: list[str] = []
    for path in sorted(workdir.rglob("*")):
        rel_parts = path.relative_to(workdir).parts
        if any(part in _IGNORED_COPY_DIRS for part in rel_parts):
            continue
        if path.is_file() and path.suffix.lower() in {".py", ".r", ".sh", ".do", ".jl", ".ipynb"}:
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


def _discover_main_commands(ctx: EvaluationContext, workdir: Path) -> list[list[str]]:
    """Discover ordered README analysis commands or fall back to a single primary entrypoint."""
    readme_cmds: list[list[str]] = []
    seen_scripts: set[str] = set()
    for _readme_path, content in ctx.read_text_files(ctx.paper.artifacts.readme_files).items():
        matches: list[tuple[int, str | None, str]] = []
        for m in _README_CMD_RE.finditer(content):
            matches.append((m.start(), m.group(1), m.group(2)))
        for m in _README_NOTEBOOK_RE.finditer(content):
            matches.append((m.start(), None, m.group(1)))
        for _pos, tool, script_raw in sorted(matches, key=lambda item: item[0]):
            if _PREP_NAME_RE.search(script_raw):
                continue
            resolved = _resolve_script_relpath(workdir, script_raw)
            if not resolved or resolved in seen_scripts:
                continue
            seen_scripts.add(resolved)
            if tool is None:
                readme_cmds.append(_command_for_script(resolved))
            else:
                norm_tool = "python" if tool == "python3" else tool
                if resolved.lower().endswith(".do") and norm_tool.startswith("stata"):
                    readme_cmds.append([norm_tool, "-b", "do", resolved])
                else:
                    readme_cmds.append([norm_tool, resolved])
    if readme_cmds:
        return readme_cmds

    # Match conventional entrypoint names in code_files / workspace
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
                return [_command_for_script(rel)]

    # Single non-test non-prep script fallback
    non_test = [rel for rel in pool if "test" not in Path(rel).stem.lower()]
    if non_test:
        return [_command_for_script(non_test[0])]
    if pool:
        return [_command_for_script(pool[0])]
    return []


def _discover_main_command(ctx: EvaluationContext, workdir: Path) -> list[str] | None:
    cmds = _discover_main_commands(ctx, workdir)
    return cmds[0] if cmds else None


def _script_from_command(cmd: list[str]) -> str:
    return next((tok for tok in reversed(cmd) if "." in tok and not tok.startswith("-")), cmd[-1])


def _run_command_sequence(
    sandbox: DockerSandboxRunner,
    image_tag: str,
    commands: list[list[str]],
    workdir: Path,
    data_mount: Path | None,
    *,
    env: dict[str, str] | None = None,
) -> tuple[SandboxRunResult, list[str]]:
    """Run one or more commands sequentially and aggregate produced artifacts/logs."""
    if len(commands) == 1:
        return (
            sandbox.run_command(image_tag, commands[0], workdir, data_mount=data_mount, env=env),
            commands[0],
        )

    created: list[str] = []
    modified: list[str] = []
    previews: dict[str, str] = {}
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    last_res: SandboxRunResult | None = None
    last_cmd = commands[0]

    for cmd in commands:
        last_cmd = cmd
        res = sandbox.run_command(image_tag, cmd, workdir, data_mount=data_mount, env=env)
        last_res = res
        created.extend(res.created_files)
        modified.extend(res.modified_files)
        previews.update(res.output_previews)
        if res.command_result.stdout:
            stdout_parts.append(res.command_result.stdout)
        if res.command_result.stderr:
            stderr_parts.append(res.command_result.stderr)
        if not res.success:
            break

    assert last_res is not None
    merged_cmd_res = last_res.command_result.model_copy(
        update={
            "stdout": "\n".join(stdout_parts)[:50_000],
            "stderr": "\n".join(stderr_parts)[:50_000],
        }
    )
    aggregated = SandboxRunResult(
        success=last_res.success,
        command=last_cmd,
        command_result=merged_cmd_res,
        created_files=sorted(set(created)),
        modified_files=sorted(set(modified)),
        output_previews=previews,
    )
    return aggregated, last_cmd


def _detect_data_mount(workdir: Path) -> Path | None:
    for dirname in ("dataverse_files", "data", "zenodo_files", "osf_files"):
        candidate = workdir / dirname
        if candidate.is_dir():
            return candidate
    return None


def _copy_sandbox_subworkspace(
    workdir: Path,
    subdir_name: str,
    *,
    target_parent: Path | None = None,
) -> Path:
    sandbox_parent = (target_parent or workdir) / ".researchability_sandbox"
    sub_dir = sandbox_parent / subdir_name
    if sub_dir.exists():
        shutil.rmtree(sub_dir)
    sub_dir.mkdir(parents=True, exist_ok=True)
    for item in workdir.iterdir():
        if item.name in _IGNORED_COPY_DIRS:
            continue
        dest = sub_dir / item.name
        if item.is_dir():
            shutil.copytree(
                item,
                dest,
                ignore=shutil.ignore_patterns(*sorted(_IGNORED_COPY_DIRS)),
            )
        else:
            shutil.copy2(item, dest)
    return sub_dir


def _copy_workspace_for_repair(workdir: Path) -> Path:
    return _copy_sandbox_subworkspace(workdir, "repaired_workspace")


def _inject_python_header_safe(source: str, inject: str) -> str:
    """Insert Python preamble after shebang, coding cookies, module docstring, and __future__."""
    lines = source.splitlines(keepends=True)
    insert_idx = 0

    try:
        tree = ast.parse(source)
        after_line = 0
        body = list(tree.body)
        if body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                after_line = max(after_line, first.end_lineno or first.lineno)
                body = body[1:]
        for stmt in body:
            if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__":
                after_line = max(after_line, stmt.end_lineno or stmt.lineno)
            else:
                break
        if after_line > 0:
            insert_idx = min(after_line, len(lines))
    except SyntaxError:
        after_line = 0

    if insert_idx == 0:
        for idx, line in enumerate(lines[:3]):
            stripped = line.strip()
            if (idx == 0 and stripped.startswith("#!")) or (
                stripped.startswith("#") and "coding" in stripped
            ):
                insert_idx = idx + 1
            else:
                break

    if insert_idx > 0 and not lines[insert_idx - 1].endswith(("\n", "\r")):
        lines[insert_idx - 1] = lines[insert_idx - 1] + "\n"
    return "".join([*lines[:insert_idx], inject, *lines[insert_idx:]])


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
    *,
    extra_known_files: list[str] | None = None,
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

    def _lookup_rel(filename: str) -> str | None:
        found = _find_workspace_file_by_name(repair_dir, filename)
        if found:
            return found
        for cand in extra_known_files or ():
            if Path(cand).name == filename:
                return Path(cand).as_posix()
        return None

    # 1. Replace hardcoded absolute file paths pointing to workspace or generated filenames
    def _replace_abs(match: re.Match[str]) -> str:
        quote, raw_path = match.group(1), match.group(2)
        filename = Path(raw_path.replace("\\", "/")).name
        found_rel = _lookup_rel(filename)
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
        found_rel = _lookup_rel(missing_name)
        if found_rel and found_rel != missing_str and missing_str in updated:
            updated = updated.replace(missing_str, found_rel)
            reasons.append(f"Updated relative path '{missing_str}' -> '{found_rel}'.")
        elif (
            not Path(missing_str).is_absolute()
            and ".." not in Path(missing_str).parts
            and ("/" in missing_str or "\\" in missing_str)
        ):
            parent_dir = (repair_dir / missing_str).parent.resolve()
            if (
                safe_resolve_under(repair_root, Path(missing_str)) is not None
                and not parent_dir.exists()
            ):
                parent_dir.mkdir(parents=True, exist_ok=True)
                if script_path.suffix.lower() == ".py" and "makedirs" not in updated:
                    rel_parent = Path(missing_str).parent.as_posix()
                    inject = f"import os\nos.makedirs({rel_parent!r}, exist_ok=True)\n"
                    updated = _inject_python_header_safe(updated, inject)
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
    try:
        patch, _ = ctx.llm.generate_structured(
            paper_id=ctx.paper.paper_id,
            gate_id="G4.5_ANALYSIS_EXECUTES_REPAIRED",
            prompt=prompt,
            schema_cls=_LLMRepairPatch,
            tier=ModelTier.FAST,
        )
    except (GeminiApiError, MissingApiKeyError):
        return None
    target_path = safe_resolve_under(repair_dir, patch.relative_file)
    if target_path is None or not target_path.is_file():
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

    ref_vals = ctx.extracted_claims.get("subsampled_reference_vals")
    cand_vals = ctx.extracted_claims.get("subsampled_candidate_vals")
    rationale = preflight.reason
    if (
        preflight.status in {GateStatus.BLOCKED_DATA_SIZE, GateStatus.BLOCKED_HARDWARE}
        and isinstance(ref_vals, list)
        and isinstance(cand_vals, list)
    ):
        dist_res = compare_distributions(
            [float(x) for x in ref_vals],
            [float(x) for x in cand_vals],
        )
        ctx.extracted_claims["distributional_verification"] = dist_res
        if dist_res.matched:
            flags.append(
                f"SUBSAMPLED_DISTRIBUTION_VERIFIED "
                f"(KS={dist_res.ks_stat}, n={dist_res.n_subsampled})"
            )
            rationale = (
                f"{preflight.reason} Subsampled distributional fallback matched reference "
                f"(KS={dist_res.ks_stat}, max_q_diff={dist_res.quantile_max_diff})."
            )
        else:
            flags.extend(dist_res.flags)

    return GateResult(
        gate_id="G4.1_SANDBOX_FEASIBLE",
        status=preflight.status,
        rationale=rationale,
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


def _maybe_run_multiseed(
    ctx: EvaluationContext,
    sandbox: DockerSandboxRunner,
    image_tag: str,
    main_cmd: list[str],
    workdir: Path,
    data_mount: Path | None,
    base_env: dict[str, str] | None = None,
) -> None:
    """Optionally run additional seeds in sandbox when multiseed check is enabled."""
    seeds_raw = ctx.extracted_claims.get("multiseed_values")
    if isinstance(seeds_raw, list) and seeds_raw:
        seeds = [int(s) for s in seeds_raw[:5]]
    elif bool(ctx.extracted_claims.get("run_multiseed_check", False)):
        seeds = [101, 202, 303]
    else:
        return

    seed_results: list[tuple[int, SandboxRunResult]] = []
    target_parent = ctx.workdir if ctx.workdir and ctx.workdir.exists() else workdir
    for seed in seeds:
        seed_dir = _copy_sandbox_subworkspace(
            workdir,
            f"multiseed_seed_{seed}",
            target_parent=target_parent,
        )
        env = dict(base_env or {})
        env["PYTHONHASHSEED"] = str(seed)
        env["RESEARCHABILITY_SEED"] = str(seed)
        res = sandbox.run_command(
            image_tag,
            main_cmd,
            seed_dir,
            data_mount=data_mount,
            env=env,
        )
        if res.success:
            seed_results.append((seed, res))
    if seed_results:
        ctx.extracted_claims["multiseed_sandbox_results"] = seed_results


def eval_g4_4_analysis_zero_touch(ctx: EvaluationContext) -> GateResult:
    """Run main analysis unmodified inside an isolated workspace copy (G4.4)."""
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
    main_cmds = _discover_main_commands(ctx, ctx.workdir)
    if not main_cmds:
        return GateResult(
            gate_id="G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH",
            status=GateStatus.FAIL,
            rationale="Could not identify an executable main analysis entrypoint.",
        )

    main_cmd = main_cmds[0]
    ctx.extracted_claims["main_command"] = main_cmd
    ctx.extracted_claims["main_commands"] = main_cmds
    data_mount = _detect_data_mount(ctx.workdir)
    zero_touch_dir = _copy_sandbox_subworkspace(ctx.workdir, "zero_touch_workspace")
    run_res, failed_or_last_cmd = _run_command_sequence(
        sandbox,
        image_tag,
        main_cmds,
        zero_touch_dir,
        data_mount,
    )
    ctx.extracted_claims["analysis_run_zero_touch"] = run_res
    ctx.extracted_claims["analysis_last_command"] = failed_or_last_cmd

    cmd_label = " && ".join(" ".join(c) for c in main_cmds)
    if run_res.success:
        ctx.extracted_claims["analysis_run_effective"] = run_res
        ctx.extracted_claims["analysis_workdir"] = zero_touch_dir
        _maybe_run_multiseed(ctx, sandbox, image_tag, main_cmd, zero_touch_dir, data_mount)
        return GateResult(
            gate_id="G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH",
            status=GateStatus.PASS,
            rationale=(f"Main analysis '{cmd_label}' completed zero-touch in offline sandbox."),
            evidence=[
                EvidenceItem(
                    source=cmd_label,
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
            f"Zero-touch analysis '{' '.join(failed_or_last_cmd)}' failed "
            f"(exit={run_res.command_result.returncode})."
        ),
        evidence=[EvidenceItem(source=" ".join(failed_or_last_cmd), excerpt=excerpt[:600])],
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

    raw_cmds = ctx.extracted_claims.get("main_commands")
    main_cmds: list[list[str]] = (
        raw_cmds
        if isinstance(raw_cmds, list) and raw_cmds
        else _discover_main_commands(ctx, ctx.workdir)
    )
    target_cmd: list[str] | None = (
        ctx.extracted_claims.get("analysis_last_command")
        or ctx.extracted_claims.get("main_command")
        or (main_cmds[0] if main_cmds else None)
    )
    if target_cmd is None or len(target_cmd) < 2:
        return GateResult(
            gate_id="G4.5_ANALYSIS_EXECUTES_REPAIRED",
            status=GateStatus.FAIL,
            rationale="No target entrypoint script available to repair.",
        )

    if not main_cmds:
        main_cmds = [target_cmd]

    sandbox = _get_sandbox(ctx)
    image_tag = str(
        ctx.extracted_claims.get("sandbox_image_tag") or sandbox.image_tag_for(ctx.paper)
    )
    repair_dir = _copy_workspace_for_repair(ctx.workdir)
    data_mount = _detect_data_mount(repair_dir)
    script_rel = _script_from_command(target_cmd)
    current_stderr = (zero_touch.command_result.stderr if zero_touch is not None else "") or (
        zero_touch.command_result.stdout if zero_touch is not None else ""
    )

    applied_diffs: list[tuple[str, str, str]] = []
    env_overrides: dict[str, str] = {}
    repaired_run: SandboxRunResult | None = None

    extra_files = list(zero_touch.created_files) if zero_touch is not None else []
    for _attempt in range(2):
        step_diffs, step_env = _apply_deterministic_repairs(
            repair_dir=repair_dir,
            script_rel=script_rel,
            stderr=current_stderr,
            extra_known_files=extra_files,
        )
        if not step_diffs and not step_env and ctx.llm is not None:
            llm_patch = _apply_llm_repair(ctx, repair_dir, script_rel, current_stderr)
            if llm_patch is not None:
                step_diffs.append(llm_patch)

        if not step_diffs and not step_env:
            break

        applied_diffs.extend(step_diffs)
        env_overrides.update(step_env)
        repaired_run, failed_cmd = _run_command_sequence(
            sandbox,
            image_tag,
            main_cmds,
            repair_dir,
            data_mount,
            env=env_overrides or None,
        )
        if repaired_run.success:
            break
        script_rel = _script_from_command(failed_cmd)
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

    cmd_label = " && ".join(" ".join(c) for c in main_cmds)
    if repaired_run.success:
        ctx.extracted_claims["analysis_run_effective"] = repaired_run
        ctx.extracted_claims["analysis_workdir"] = repair_dir
        _maybe_run_multiseed(
            ctx,
            sandbox,
            image_tag,
            main_cmds[0],
            repair_dir,
            data_mount,
            base_env=env_overrides or None,
        )
        return GateResult(
            gate_id="G4.5_ANALYSIS_EXECUTES_REPAIRED",
            status=GateStatus.PASS,
            rationale=(
                f"Main analysis '{cmd_label}' succeeded after logged minor repair "
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
            f"Repaired analysis '{cmd_label}' still failed "
            f"(exit={repaired_run.command_result.returncode})."
        ),
        evidence=[*evidence, EvidenceItem(source=cmd_label, excerpt=err_excerpt[:500])],
        flags=["REPAIR_FAILED"],
    )


STAGE4_EVALUATORS = {
    "G4.1_SANDBOX_FEASIBLE": eval_g4_1_sandbox_feasible,
    "G4.2_ENV_BUILDS": eval_g4_2_env_builds,
    "G4.3_PREP_EXECUTES": eval_g4_3_prep_executes,
    "G4.4_ANALYSIS_EXECUTES_ZERO_TOUCH": eval_g4_4_analysis_zero_touch,
    "G4.5_ANALYSIS_EXECUTES_REPAIRED": eval_g4_5_analysis_repaired,
}
