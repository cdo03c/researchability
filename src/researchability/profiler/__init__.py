"""Deterministic data profiling and forensic numeric consistency checks."""

from researchability.profiler.data_profiler import DataFileProfile, profile_data_file
from researchability.profiler.forensics import (
    GrimResult,
    StatcheckResult,
    check_grim,
    check_stat_consistency,
    extract_claimed_sample_sizes,
    run_forensic_checks,
)

__all__ = [
    "DataFileProfile",
    "GrimResult",
    "StatcheckResult",
    "check_grim",
    "check_stat_consistency",
    "extract_claimed_sample_sizes",
    "profile_data_file",
    "run_forensic_checks",
]
