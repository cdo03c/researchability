"""Core Pydantic domain models for papers, artifacts, binary gates, and audit reports."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class GateStatus(StrEnum):
    """Possible evaluation states for a binary reproducibility gate."""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED_HARDWARE = "BLOCKED_HARDWARE"
    BLOCKED_ACCESS = "BLOCKED_ACCESS"
    BLOCKED_DATA_SIZE = "BLOCKED_DATA_SIZE"
    N_A = "N/A"

    @property
    def is_blocked(self) -> bool:
        """Return True when execution/evaluation was blocked by external constraints."""
        return self in {
            GateStatus.BLOCKED_HARDWARE,
            GateStatus.BLOCKED_ACCESS,
            GateStatus.BLOCKED_DATA_SIZE,
        }

    @property
    def is_scorable(self) -> bool:
        """Return True when the gate contributed a binary PASS or FAIL verdict."""
        return self in {GateStatus.PASS, GateStatus.FAIL}


class ProblemDomain(StrEnum):
    """Reproducibility problem taxonomy mapped across stages."""

    TRIAGE = "triage"
    TRANSPARENCY_REPORTING = "transparency_reporting"
    INCENTIVE_BIAS = "incentive_bias"
    MATERIALS_VARIABILITY = "materials_variability"
    STUDY_DESIGN = "study_design"
    STATISTICAL_RIGOR = "statistical_rigor"
    DATA_INTEGRITY = "data_integrity"
    EXECUTION_OUTPUT = "execution_output"


class FieldProfile(StrEnum):
    """Supported disciplinary gate overlays."""

    CORE = "core"
    ML_CS = "ml_cs"
    BIOINFORMATICS = "bioinformatics"
    BIOMEDICINE_WETLAB = "biomedicine_wetlab"
    PSYCHOLOGY_SOCIAL = "psychology_social"
    ECONOMICS = "economics"


class EvaluatorKind(StrEnum):
    """Indicates how a gate is evaluated for provenance transparency."""

    DETERMINISTIC = "deterministic"
    LLM_ASSISTED = "llm_assisted"
    HYBRID = "hybrid"


class DependencyMode(StrEnum):
    """How multiple upstream gate prerequisites combine."""

    ALL = "all"
    ANY = "any"


class ArtifactStatus(StrEnum):
    """Resolution outcome for an external code or data link."""

    RESOLVED = "RESOLVED"
    BROKEN_404 = "BROKEN_404"
    STUB_ONLY = "STUB_ONLY"
    BLOCKED_ACCESS = "BLOCKED_ACCESS"
    BLOCKED_DATA_SIZE = "BLOCKED_DATA_SIZE"
    UNRESOLVED = "UNRESOLVED"


class EvidenceItem(BaseModel):
    """Traceable piece of evidence supporting a gate verdict."""

    source: str = Field(..., description="File path, URL, paper section, or command.")
    excerpt: str = Field(..., description="Quoted snippet, metric, or log excerpt.")
    note: str | None = Field(default=None, description="Optional explanation or diff.")


class ArtifactLink(BaseModel):
    """Resolved or attempted code/data artifact linked from a paper."""

    url: str
    kind: str = Field(default="unknown", description="code | data | protocol | prereg | other")
    host: str = Field(
        default="unknown",
        description="github | zenodo | dataverse | huggingface | osf | other",
    )
    status: ArtifactStatus = ArtifactStatus.UNRESOLVED
    local_path: str | None = None
    size_bytes: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactManifest(BaseModel):
    """Collection of artifacts discovered for a paper."""

    links: list[ArtifactLink] = Field(default_factory=list)
    code_files: list[str] = Field(default_factory=list)
    data_files: list[str] = Field(default_factory=list)
    manifest_files: list[str] = Field(default_factory=list)
    readme_files: list[str] = Field(default_factory=list)


class PaperBundle(BaseModel):
    """Normalized representation of a research paper and its associated artifacts."""

    paper_id: str = Field(
        ...,
        description="Canonical identifier (e.g., arxiv:2401.00001 or doi:10.7910/...).",
    )
    source_repo: str = Field(..., description="Origin repository (arxiv | dataverse | local).")
    title: str
    authors: list[str] = Field(default_factory=list)
    published_year: int | None = None
    doi: str | None = None
    primary_profile: FieldProfile = FieldProfile.ML_CS
    abstract: str = ""
    full_text: str = ""
    sections: dict[str, str] = Field(default_factory=dict)
    tables: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: ArtifactManifest = Field(default_factory=ArtifactManifest)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GateDefinition(BaseModel):
    """Declarative specification of a single binary gate."""

    id: str
    stage: int = Field(..., ge=0, le=5)
    name: str
    description: str
    problem_domain: ProblemDomain
    evaluator_kind: EvaluatorKind
    profiles: list[FieldProfile] = Field(default_factory=lambda: [FieldProfile.CORE])
    depends_on: list[str] = Field(default_factory=list)
    dependency_mode: DependencyMode = DependencyMode.ALL
    weight: float = Field(default=1.0, ge=0.0)


class GateResult(BaseModel):
    """Outcome of evaluating one gate on a paper."""

    gate_id: str
    status: GateStatus
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    rationale: str = ""
    evidence: list[EvidenceItem] = Field(default_factory=list)
    flags: list[str] = Field(
        default_factory=list,
        description="Forensic or quality warnings for human review.",
    )
    model_id: str | None = None
    prompt_version: str | None = None


class DomainScore(BaseModel):
    """Aggregated score breakdown for one reproducibility problem domain."""

    domain: ProblemDomain
    passed: int = 0
    failed: int = 0
    blocked: int = 0
    not_applicable: int = 0
    weighted_passed: float = 0.0
    weighted_total: float = 0.0
    pass_rate: float | None = Field(
        default=None,
        description="Unweighted PASS / (PASS + FAIL), or None when no scorable gates apply.",
    )
    weighted_pass_rate: float | None = Field(
        default=None,
        description="Weighted pass rate over scorable gates, or None when total weight is 0.",
    )


class AuditReport(BaseModel):
    """Complete reproducibility audit and score summary for a paper."""

    paper_id: str
    source_repo: str
    profile: FieldProfile
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    funnel_depth: int = Field(..., ge=0, le=5)
    composite_score: float | None = Field(
        default=None,
        description=(
            "Weighted pass rate across all scorable gates (0.0-1.0), or None if none scorable."
        ),
    )
    core_composite_score: float | None = Field(
        default=None,
        description=(
            "Weighted pass rate strictly across FieldProfile.CORE scorable gates for "
            "apples-to-apples cross-field comparisons."
        ),
    )
    unweighted_pass_rate: float | None = None
    zero_touch_reproducible: bool | None = None
    repaired_reproducible: bool | None = None
    gate_results: list[GateResult] = Field(default_factory=list)
    domain_scores: dict[ProblemDomain, DomainScore] = Field(default_factory=dict)
    human_review_flags: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
