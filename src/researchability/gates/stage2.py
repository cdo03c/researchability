"""Stage 2 evaluators: materials, software environment, and technical variability."""

from __future__ import annotations

import re

from researchability.gates.base import EvaluationContext, run_llm_gate
from researchability.models import EvidenceItem, GateResult, GateStatus

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
    """Verify README files exist and document execution entrypoints."""
    if not ctx.paper.artifacts.readme_files:
        return GateResult(
            gate_id="G2.1_README_ENTRYPOINT",
            status=GateStatus.FAIL,
            rationale="No README file found in repository.",
        )

    texts = ctx.read_text_files(ctx.paper.artifacts.readme_files)
    for path, body in texts.items():
        match = _README_RUN_RE.search(body)
        if match:
            return GateResult(
                gate_id="G2.1_README_ENTRYPOINT",
                status=GateStatus.PASS,
                rationale="README documents run commands or execution order.",
                evidence=[EvidenceItem(source=path, excerpt=match.group(0))],
            )

    return GateResult(
        gate_id="G2.1_README_ENTRYPOINT",
        status=GateStatus.FAIL,
        rationale="README present but lacks clear entrypoint/execution commands.",
    )


def eval_g2_2_env_manifest(ctx: EvaluationContext) -> GateResult:
    """Check whether a software dependency manifest or Dockerfile exists."""
    manifests = ctx.paper.artifacts.manifest_files
    if manifests:
        return GateResult(
            gate_id="G2.2_ENV_MANIFEST",
            status=GateStatus.PASS,
            rationale=f"Found {len(manifests)} dependency/environment manifest(s).",
            evidence=[EvidenceItem(source="manifests", excerpt=", ".join(manifests[:5]))],
        )
    return GateResult(
        gate_id="G2.2_ENV_MANIFEST",
        status=GateStatus.FAIL,
        rationale=(
            "No dependency manifest (requirements.txt, pyproject.toml, "
            "environment.yml, renv.lock, Dockerfile) found."
        ),
    )


def eval_g2_3_versions_pinned(ctx: EvaluationContext) -> GateResult:
    """Verify that dependency manifests or lockfiles pin package/image versions."""
    manifests = ctx.paper.artifacts.manifest_files
    for name in manifests:
        if name.rsplit("/", maxsplit=1)[-1].lower() in _LOCKFILES:
            return GateResult(
                gate_id="G2.3_VERSIONS_PINNED",
                status=GateStatus.PASS,
                rationale="Lockfile detected with exact dependency pins.",
                evidence=[EvidenceItem(source="manifests", excerpt=name)],
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
            )

    return GateResult(
        gate_id="G2.3_VERSIONS_PINNED",
        status=GateStatus.FAIL,
        rationale="Environment manifests do not pin explicit package or container tag versions.",
        flags=["Unpinned dependencies may drift across environments"],
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
