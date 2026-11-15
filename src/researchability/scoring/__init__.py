"""Scoring aggregation, dependency propagation, and funnel depth calculation."""

from researchability.scoring.aggregator import (
    compute_audit_report,
    render_markdown_summary,
    resolve_dependency_override,
)

__all__ = [
    "compute_audit_report",
    "render_markdown_summary",
    "resolve_dependency_override",
]
