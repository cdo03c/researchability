"""Deterministic data profiling, repository hygiene, and forensic numeric consistency checks."""

from researchability.profiler.data_profiler import DataFileProfile, profile_data_file
from researchability.profiler.forensics import (
    GrimResult,
    StatcheckResult,
    check_effect_size_bounds,
    check_grim,
    check_stat_consistency,
    check_table_stars_and_cis,
    extract_claimed_sample_sizes,
    run_forensic_checks,
)
from researchability.profiler.hygiene import (
    DependencyAuditResult,
    DuplicateFileGroup,
    OutputCollision,
    ReadmeInventoryAudit,
    ScriptPathAuditResult,
    audit_dependencies,
    audit_readme_inventory,
    audit_script_paths,
    detect_duplicate_files,
    detect_output_collisions,
    scan_code_sentinel_risks,
)

__all__ = [
    "DataFileProfile",
    "DependencyAuditResult",
    "DuplicateFileGroup",
    "GrimResult",
    "OutputCollision",
    "ReadmeInventoryAudit",
    "ScriptPathAuditResult",
    "StatcheckResult",
    "audit_dependencies",
    "audit_readme_inventory",
    "audit_script_paths",
    "check_effect_size_bounds",
    "check_grim",
    "check_stat_consistency",
    "check_table_stars_and_cis",
    "detect_duplicate_files",
    "detect_output_collisions",
    "extract_claimed_sample_sizes",
    "profile_data_file",
    "run_forensic_checks",
    "scan_code_sentinel_risks",
]
