"""Stage 2 evaluators: materials, software environment, and technical variability."""

from __future__ import annotations

import re

from researchability.gates.base import EvaluationContext, run_llm_gate
from researchability.models import EvidenceItem, GateResult, GateStatus
from researchability.profiler.hygiene import (
    DependencyAuditResult,
    audit_dependencies,
    audit_readme_inventory,
    audit_script_paths,
    detect_duplicate_files,
    detect_output_collisions,
)
from researchability.profiler.spec_checks import (
    audit_constants_and_diagnostics,
    audit_script_isolation_and_stata,
)

_README_RUN_RE = re.compile(
    r"(python\s+|rscript\s+|pytest|uv\s+run|make\s+|docker\s+|bash\s+|sh\s+|usage|reproduc|run\s+the)",
    re.IGNORECASE,
)
_PINNED_SPEC_RE = re.compile(r"(==|===|~=|\bFROM\s+\S+:(?!latest)\S+)")
_LOCKFILES = {"uv.lock", "poetry.lock", "pipfile.lock", "renv.lock"}
_SEED_CODE_RE = re.compile(
    r"(random\.seed|np\.random\.seed|torch\.manual_seed|tf\.random\.set_seed|"
    r"set\.seed|PYTHONHASHSEED|seed\s*=|deterministic\s*=\s*True)",
    re.IGNORECASE,
)
_HARDWARE_RE = re.compile(
    r"\b(nvidia|a100|h100|v100|rtx\s*\d+|gpu|tpu|cpu|gb\s*ram|vram|core[s]?|hours?|minutes?)\b",
    re.IGNORECASE,
)
_BIO_REF_RE = re.compile(
    r"\b(GRCh3[78]|hg19|hg38|mm10|mm39|GENCODE\s*v?\d+|Ensembl\s*(?:release\s*)?\d+|"
    r"GSE\d+|SRR\d+|PRJNA\d+|RefSeq|UniProt)\b",
    re.IGNORECASE,
)


def eval_g2_1_readme_entrypoint(ctx: EvaluationContext) -> GateResult:
    """Verify README files exist, document entrypoints, and pass inventory/collision hygiene."""
    if not ctx.paper.artifacts.readme_files:
        return GateResult(
            gate_id="G2.1_README_ENTRYPOINT",
            status=GateStatus.FAIL,
            rationale="No README file found in repository.",
        )

    texts = ctx.read_text_files(ctx.paper.artifacts.readme_files)
    code_texts = ctx.read_text_files(ctx.paper.artifacts.code_files)
    inv = audit_readme_inventory(
        texts,
        ctx.paper.artifacts.code_files,
        ctx.paper.artifacts.data_files,
    )
    dups = detect_duplicate_files(
        ctx.workdir,
        [*ctx.paper.artifacts.code_files, *ctx.paper.artifacts.data_files],
    )
    collisions = detect_output_collisions(code_texts)
    all_known = [
        *ctx.paper.artifacts.code_files,
        *ctx.paper.artifacts.data_files,
        *ctx.paper.artifacts.manifest_files,
        *ctx.paper.artifacts.readme_files,
    ]
    path_audit = audit_script_paths(ctx.workdir, code_texts, all_known)

    flags: list[str] = list(inv.flags)
    divergent_dups = [d for d in dups if d.divergent_content]
    for d in dups:
        if d.divergent_content:
            flags.append(f"DUPLICATE_DIVERGENT_FILE: {d.basename} ({', '.join(d.paths[:4])})")
        else:
            flags.append(f"DUPLICATE_BASENAME: {d.basename} ({', '.join(d.paths[:4])})")
    for col in collisions:
        flags.append(f"OUTPUT_COLLISION: {col.target} overwritten by {', '.join(col.writers[:4])}")
    flags.extend(path_audit.flags)

    isolation_flags = audit_script_isolation_and_stata(code_texts)
    const_flags = audit_constants_and_diagnostics(code_texts)
    for item in [*isolation_flags, *const_flags]:
        if item not in flags:
            flags.append(item)

    ctx.extracted_claims["hygiene_audit"] = {
        "stale_references": inv.stale_references,
        "non_plain_text_readmes": inv.non_plain_text_readmes,
        "undocumented_code_files": inv.undocumented_code_files,
        "undocumented_data_files": inv.undocumented_data_files,
        "competing_entrypoints": inv.competing_entrypoints,
        "divergent_duplicates": [f"{d.basename}: {', '.join(d.paths)}" for d in divergent_dups],
        "output_collisions": [f"{c.target} <- {', '.join(c.writers)}" for c in collisions],
        "missing_input_references": path_audit.missing_input_references,
        "hardcoded_dirs": path_audit.hardcoded_dirs,
        "script_hygiene_flags": [*isolation_flags, *const_flags],
    }

    non_plain_set = set(inv.non_plain_text_readmes)
    run_match: re.Match[str] | None = None
    run_path = ""
    for path, body in texts.items():
        if path in non_plain_set:
            continue
        match = _README_RUN_RE.search(body)
        if match:
            run_match = match
            run_path = path
            break

    blocking_reasons: list[str] = []
    if inv.non_plain_text_readmes and run_match is None:
        non_plain_str = ", ".join(inv.non_plain_text_readmes)
        blocking_reasons.append(
            f"non-plain-text README without plain-text instructions ({non_plain_str})"
        )
    elif run_match is None:
        blocking_reasons.append("README lacks clear entrypoint/execution commands")
    if inv.stale_references:
        blocking_reasons.append(
            f"README references missing file(s): {', '.join(inv.stale_references[:4])}"
        )
    if divergent_dups:
        dup_names = ", ".join(d.basename for d in divergent_dups[:3])
        blocking_reasons.append(f"divergent duplicate files share basename ({dup_names})")
    if collisions:
        col_targets = ", ".join(c.target for c in collisions[:3])
        blocking_reasons.append(f"repeated output file overwrite collision ({col_targets})")

    if blocking_reasons:
        evidence = [EvidenceItem(source="hygiene_audit", excerpt="; ".join(blocking_reasons))]
        if run_match is not None:
            evidence.append(EvidenceItem(source=run_path, excerpt=run_match.group(0)))
        return GateResult(
            gate_id="G2.1_README_ENTRYPOINT",
            status=GateStatus.FAIL,
            rationale="README/repository hygiene check failed: "
            + "; ".join(blocking_reasons)
            + ".",
            evidence=evidence,
            flags=flags,
        )

    assert run_match is not None
    return GateResult(
        gate_id="G2.1_README_ENTRYPOINT",
        status=GateStatus.PASS,
        rationale="README documents run commands or execution order.",
        evidence=[EvidenceItem(source=run_path, excerpt=run_match.group(0))],
        flags=flags,
    )


def _get_or_compute_dep_audit(ctx: EvaluationContext) -> DependencyAuditResult:
    cached = ctx.extracted_claims.get("dependency_audit")
    if isinstance(cached, DependencyAuditResult):
        return cached
    code_texts = ctx.read_text_files(ctx.paper.artifacts.code_files)
    manifest_texts = ctx.read_text_files(ctx.paper.artifacts.manifest_files)
    result = audit_dependencies(
        code_texts,
        manifest_texts,
        check_registry=bool(ctx.extracted_claims.get("check_registry", False)),
        http_get=ctx.extracted_claims.get("registry_http_get"),
    )
    ctx.extracted_claims["dependency_audit"] = result
    return result


def eval_g2_2_env_manifest(ctx: EvaluationContext) -> GateResult:
    """Check whether a software dependency manifest or Dockerfile exists."""
    dep_audit = _get_or_compute_dep_audit(ctx)
    manifests = ctx.paper.artifacts.manifest_files
    if manifests:
        return GateResult(
            gate_id="G2.2_ENV_MANIFEST",
            status=GateStatus.PASS,
            rationale=f"Found {len(manifests)} dependency/environment manifest(s).",
            evidence=[EvidenceItem(source="manifests", excerpt=", ".join(manifests[:5]))],
            flags=list(dep_audit.flags),
        )
    return GateResult(
        gate_id="G2.2_ENV_MANIFEST",
        status=GateStatus.FAIL,
        rationale=(
            "No dependency manifest (requirements.txt, pyproject.toml, "
            "environment.yml, renv.lock, Dockerfile) found."
        ),
        flags=list(dep_audit.flags),
    )


def eval_g2_3_versions_pinned(ctx: EvaluationContext) -> GateResult:
    """Verify dependency manifests pin versions and have no unpinned remotes or missing packages."""
    dep_audit = _get_or_compute_dep_audit(ctx)
    blocking: list[str] = []
    if dep_audit.archived_or_missing_packages:
        blocking.append(
            "archived/missing registry packages: "
            + ", ".join(dep_audit.archived_or_missing_packages[:5])
        )
    if dep_audit.unpinned_remote_packages:
        blocking.append(
            "unpinned remote installs: " + ", ".join(dep_audit.unpinned_remote_packages[:5])
        )
    if blocking:
        return GateResult(
            gate_id="G2.3_VERSIONS_PINNED",
            status=GateStatus.FAIL,
            rationale="Dependency pinning/liveness check failed: " + "; ".join(blocking) + ".",
            evidence=[EvidenceItem(source="dependencies", excerpt="; ".join(blocking))],
            flags=list(dep_audit.flags),
        )

    manifests = ctx.paper.artifacts.manifest_files
    for name in manifests:
        if name.rsplit("/", maxsplit=1)[-1].lower() in _LOCKFILES:
            return GateResult(
                gate_id="G2.3_VERSIONS_PINNED",
                status=GateStatus.PASS,
                rationale="Lockfile detected with exact dependency pins.",
                evidence=[EvidenceItem(source="manifests", excerpt=name)],
                flags=list(dep_audit.flags),
            )

    texts = ctx.read_text_files(manifests)
    for path, body in texts.items():
        match = _PINNED_SPEC_RE.search(body)
        if match:
            return GateResult(
                gate_id="G2.3_VERSIONS_PINNED",
                status=GateStatus.PASS,
                rationale="Version pins found in environment manifest.",
                evidence=[EvidenceItem(source=path, excerpt=match.group(0))],
                flags=list(dep_audit.flags),
            )

    return GateResult(
        gate_id="G2.3_VERSIONS_PINNED",
        status=GateStatus.FAIL,
        rationale="Environment manifests do not pin explicit package or container tag versions.",
        flags=[*dep_audit.flags, "Unpinned dependencies may drift across environments"],
    )


def eval_g2_4_stochastic_controls(ctx: EvaluationContext) -> GateResult:
    """Check whether random seeds or determinism controls are implemented or documented."""
    code_texts = ctx.read_text_files(ctx.paper.artifacts.code_files)
    for path, content in code_texts.items():
        match = _SEED_CODE_RE.search(content)
        if match:
            return GateResult(
                gate_id="G2.4_STOCHASTIC_CONTROLS",
                status=GateStatus.PASS,
                rationale="Random seed or deterministic flag found in code.",
                evidence=[EvidenceItem(source=path, excerpt=match.group(0))],
            )

    if re.search(
        r"\b(random seed|seed\s*=|across \d+ seeds|deterministic)\b",
        ctx.manuscript_snippet(),
        re.IGNORECASE,
    ):
        return GateResult(
            gate_id="G2.4_STOCHASTIC_CONTROLS",
            status=GateStatus.PASS,
            rationale="Stochastic seed controls documented in manuscript.",
            evidence=[EvidenceItem(source="manuscript", excerpt="seed/determinism documented")],
        )

    return GateResult(
        gate_id="G2.4_STOCHASTIC_CONTROLS",
        status=GateStatus.FAIL,
        rationale="No random seed or stochastic control documentation detected.",
        flags=["Missing seed/determinism controls"],
    )


def eval_g2_5_hardware_specs(ctx: EvaluationContext) -> GateResult:
    """Check whether compute/hardware specifications are disclosed."""

    def fallback(c: EvaluationContext) -> GateResult:
        hits = list(dict.fromkeys(m.lower() for m in _HARDWARE_RE.findall(c.manuscript_snippet())))
        passed = len(hits) >= 1
        return GateResult(
            gate_id="G2.5_HARDWARE_SPECS",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            confidence=0.8,
            rationale=(
                "Hardware/compute details disclosed."
                if passed
                else "No hardware, GPU/CPU, or runtime specifications disclosed."
            ),
            evidence=[EvidenceItem(source="manuscript", excerpt=", ".join(hits[:5]))]
            if hits
            else [],
        )

    return run_llm_gate(
        ctx,
        gate_id="G2.5_HARDWARE_SPECS",
        prompt=(
            "Does the paper or README disclose the hardware/compute requirements "
            "(CPU, GPU model, RAM, or approximate runtime)?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        fallback=fallback,
    )


def eval_g2_6_bio_reference_pinned(ctx: EvaluationContext) -> GateResult:
    """Verify bioinformatics reference genomes, annotations, and accessions are pinned."""

    def fallback(c: EvaluationContext) -> GateResult:
        hits = list(dict.fromkeys(_BIO_REF_RE.findall(c.manuscript_snippet())))
        passed = len(hits) >= 1
        return GateResult(
            gate_id="G2.6_BIO_REFERENCE_PINNED",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            confidence=0.85,
            rationale=(
                "Pinned biological reference build or accession detected."
                if passed
                else "No pinned reference genome build, annotation release, or accession found."
            ),
            evidence=[EvidenceItem(source="manuscript", excerpt=", ".join(hits[:5]))]
            if hits
            else [],
        )

    return run_llm_gate(
        ctx,
        gate_id="G2.6_BIO_REFERENCE_PINNED",
        prompt=(
            "Does this bioinformatics paper specify pinned reference genome builds "
            "(e.g., GRCh38, hg38), annotation releases (GENCODE/Ensembl), or public "
            "accession IDs (GEO/SRA)?\n\n"
            f"{ctx.manuscript_snippet()}"
        ),
        fallback=fallback,
    )


STAGE2_EVALUATORS = {
    "G2.1_README_ENTRYPOINT": eval_g2_1_readme_entrypoint,
    "G2.2_ENV_MANIFEST": eval_g2_2_env_manifest,
    "G2.3_VERSIONS_PINNED": eval_g2_3_versions_pinned,
    "G2.4_STOCHASTIC_CONTROLS": eval_g2_4_stochastic_controls,
    "G2.5_HARDWARE_SPECS": eval_g2_5_hardware_specs,
    "G2.6_BIO_REFERENCE_PINNED": eval_g2_6_bio_reference_pinned,
}
