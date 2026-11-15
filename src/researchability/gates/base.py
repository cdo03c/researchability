"""Shared context, LLM helper, and workspace file utilities for gate evaluators."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from researchability.llm.client import (
    GeminiApiError,
    GeminiClient,
    MissingApiKeyError,
    ModelTier,
)
from researchability.models import (
    EvidenceItem,
    FieldProfile,
    GateResult,
    GateStatus,
    PaperBundle,
)


class LLMGateVerdict(BaseModel):
    """Structured output schema returned by Gemini for binary gate checks."""

    passed: bool
    confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    rationale: str
    evidence_source: str = Field(default="manuscript")
    evidence_excerpt: str = Field(default="")
    flags: list[str] = Field(default_factory=list)


@dataclass
class EvaluationContext:
    """Shared state passed across Stage 0-5 gate evaluators for one paper."""

    paper: PaperBundle
    workdir: Path | None = None
    llm: GeminiClient | None = None
    profile: FieldProfile | None = None
    extracted_claims: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_profile(self) -> FieldProfile:
        """Return the active field profile for evaluation, falling back to paper.primary_profile."""
        return self.profile or self.paper.primary_profile

    def candidate_roots(self) -> list[Path]:
        """Return existing local directories holding artifacts for this paper."""
        roots: list[Path] = []
        if self.workdir and self.workdir.exists():
            roots.append(self.workdir)
            for sub in (self.workdir / "dataverse_files", self.workdir / "repos"):
                if sub.exists():
                    roots.append(sub)
        for link in self.paper.artifacts.links:
            if link.local_path:
                p = Path(link.local_path)
                if p.exists() and p not in roots:
                    roots.append(p)
        return roots

    def resolve_relative_files(self, rel_paths: list[str]) -> list[Path]:
        """Find matching existing Path objects for relative manifest paths."""
        found: list[Path] = []
        seen: set[Path] = set()
        roots = self.candidate_roots()
        for rel in rel_paths:
            for root in roots:
                direct = root / rel
                if direct.is_file() and direct not in seen:
                    seen.add(direct)
                    found.append(direct)
                    break
                # Also inspect one level down (e.g., cloned repo directory under repos/)
                if root.name == "repos":
                    for child in root.iterdir():
                        nested = child / rel
                        if nested.is_file() and nested not in seen:
                            seen.add(nested)
                            found.append(nested)
                            break
        return found

    def read_text_files(self, rel_paths: list[str], max_chars: int = 20_000) -> dict[str, str]:
        """Read UTF-8 text snippets for relative workspace paths."""
        contents: dict[str, str] = {}
        for path in self.resolve_relative_files(rel_paths):
            try:
                contents[str(path)] = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
            except OSError:
                continue
        return contents

    def manuscript_snippet(self, max_chars: int = 16_000) -> str:
        """Return bounded manuscript text prioritizing abstract, methods, and results."""
        parts: list[str] = [f"Title: {self.paper.title}"]
        if self.paper.abstract:
            parts.append(f"Abstract:\n{self.paper.abstract}")
        if self.paper.sections:
            for name, body in self.paper.sections.items():
                parts.append(f"Section [{name}]:\n{body[:4000]}")
        elif self.paper.full_text:
            parts.append(f"Full Text:\n{self.paper.full_text[:max_chars]}")
        return "\n\n".join(parts)[:max_chars]


FallbackEvaluator = Callable[[EvaluationContext], GateResult]


def run_llm_gate(
    ctx: EvaluationContext,
    *,
    gate_id: str,
    prompt: str,
    tier: ModelTier = ModelTier.FAST,
    prompt_version: str = "v1",
    fallback: FallbackEvaluator | None = None,
) -> GateResult:
    """Evaluate a gate via GeminiClient when configured, otherwise run deterministic fallback."""
    if ctx.llm is None:
        if fallback is not None:
            return fallback(ctx)
        return GateResult(
            gate_id=gate_id,
            status=GateStatus.N_A,
            rationale="Gemini client not configured and no deterministic fallback provided.",
        )

    try:
        verdict, meta = ctx.llm.generate_structured(
            paper_id=ctx.paper.paper_id,
            gate_id=gate_id,
            prompt=prompt,
            schema_cls=LLMGateVerdict,
            prompt_version=prompt_version,
            tier=tier,
            system_instruction=(
                "You are a strict scientific reproducibility auditor. Return PASS only when "
                "explicit evidence is present in the provided text; cite exact excerpts."
            ),
        )
    except (GeminiApiError, MissingApiKeyError) as exc:
        if fallback is not None:
            fb_res = fallback(ctx)
            return fb_res.model_copy(
                update={
                    "flags": [*fb_res.flags, "LLM_FALLBACK_USED"],
                    "rationale": f"{fb_res.rationale} (LLM fallback after error: {exc})",
                }
            )
        return GateResult(
            gate_id=gate_id,
            status=GateStatus.N_A,
            rationale=f"LLM evaluation unavailable for {gate_id}: {exc}",
            flags=["LLM_CALL_FAILED"],
        )
    evidence = (
        [EvidenceItem(source=verdict.evidence_source, excerpt=verdict.evidence_excerpt)]
        if verdict.evidence_excerpt
        else []
    )
    return GateResult(
        gate_id=gate_id,
        status=GateStatus.PASS if verdict.passed else GateStatus.FAIL,
        confidence=verdict.confidence,
        rationale=verdict.rationale,
        evidence=evidence,
        flags=verdict.flags,
        model_id=meta.model_id,
        prompt_version=meta.prompt_version,
    )
