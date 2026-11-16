"""Tests for data profiling and forensic numeric consistency screening."""

from __future__ import annotations

import json
import struct
from pathlib import Path

from researchability.profiler import (
    check_effect_size_bounds,
    check_grim,
    check_stat_consistency,
    check_table_stars_and_cis,
    extract_claimed_sample_sizes,
    profile_data_file,
    run_forensic_checks,
)


def test_profile_csv_json_and_npy(tmp_path: Path) -> None:
    csv_path = tmp_path / "cohort.csv"
    csv_path.write_text("id,score\n1,10.0\n2,20.0\n3,30.0\n", encoding="utf-8")
    csv_prof = profile_data_file(csv_path)
    assert csv_prof.parseable is True
    assert csv_prof.row_count == 3
    assert csv_prof.column_count == 2
    assert csv_prof.numeric_means["score"] == 20.0

    json_path = tmp_path / "records.jsonl"
    json_path.write_text(
        json.dumps({"a": 1}) + "\n" + json.dumps({"a": 2, "b": 3}) + "\n",
        encoding="utf-8",
    )
    json_prof = profile_data_file(json_path)
    assert json_prof.parseable is True
    assert json_prof.row_count == 2
    assert json_prof.columns == ["a", "b"]

    # Minimal valid .npy v1.0 header with shape (12, 4)
    header = "{'descr': '<f8', 'fortran_order': False, 'shape': (12, 4), }"
    pad = (16 - ((10 + len(header) + 1) % 16)) % 16
    header_bytes = (header + " " * pad + "\n").encode("latin1")
    npy_path = tmp_path / "matrix.npy"
    npy_path.write_bytes(b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header_bytes)) + header_bytes)
    npy_prof = profile_data_file(npy_path)
    assert npy_prof.parseable is True
    assert npy_prof.row_count == 12
    assert npy_prof.column_count == 4


def test_grim_consistency_detects_impossible_integer_mean() -> None:
    # With N=10, integer scores can produce 3.50 (sum=35) but never 3.55 (sum=35.5)
    ok = check_grim("3.50", 10)
    bad = check_grim("3.55", 10)
    assert ok.consistent is True
    assert bad.consistent is False
    assert "35.5" in bad.reason


def test_statcheck_catches_planted_p_value_and_decision_errors() -> None:
    # Valid: t(28) = 2.05 -> two-tailed p ~ 0.0498 < 0.05
    # Planted decision error: t(28) = 0.50 -> p ~ 0.621, falsely reported as p = 0.012
    text = (
        "Experiment A showed t(28) = 2.05, p = 0.0498. "
        "Experiment B reported t(28) = 0.50, p = 0.012 and z = 2.58, p = 0.010."
    )
    results = check_stat_consistency(text)
    assert len(results) == 3
    assert results[0].consistent is True
    assert results[1].consistent is False
    assert results[1].decision_error is True
    assert results[2].consistent is True

    summary = run_forensic_checks(text)
    assert len(summary.flags) == 1
    assert "decision error" in summary.flags[0]


def test_extract_claimed_sample_sizes() -> None:
    ns = extract_claimed_sample_sizes("We evaluated N = 1,200 patients across 300 samples.")
    assert ns == [1200, 300]


def test_table_stars_and_ci_forensics() -> None:
    text = (
        "Table 2 coefficients:\n"
        "Treat A = 0.05*** (0.20)\n"
        "Treat B = 0.50*** (0.10)\n"
        "Treat C = 1.20*** (0.30)\n"
        "Treat D = 0.80 (0.15)\n"
        "beta = 0.42, 95% CI [0.10, 0.30] and 95% CI [0.65, 0.25]."
    )
    flags = check_table_stars_and_cis(text)
    assert any("Significance star mismatch" in f and "0.05***" in f for f in flags)
    assert any("Missing significance stars" in f and "0.80" in f for f in flags)
    assert any("Confidence interval mismatch" in f and "0.42" in f for f in flags)
    assert any("Inverted confidence interval bounds" in f for f in flags)


def test_effect_size_bounds_and_paired_grim_in_summary() -> None:
    text = (
        "Participants scored mean = 3.55, N = 10. "
        "Treatment effect Cohen's d = 4.20, Pearson r = 1.15, R^2 = 1.08, SD = -0.35, p = 1.25."
    )
    bound_flags = check_effect_size_bounds(text)
    assert len(bound_flags) == 5

    summary = run_forensic_checks(text)
    assert len(summary.grim_results) == 1
    assert summary.grim_results[0].consistent is False
    assert any("GRIM inconsistency" in f for f in summary.flags)
    assert len(summary.flags) == 6
