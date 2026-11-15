"""Pre-analysis plan diffing, distributional verification, multi-seed, and perturbation checks."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from researchability.connectors.base import create_safe_http_client

if TYPE_CHECKING:
    from researchability.profiler.data_profiler import DataFileProfile

_OSF_REG_URL_RE = re.compile(r"osf\.io/(?:registrations/)?([a-z0-9]{5})\b", re.IGNORECASE)
_PVAL_INLINE_RE = re.compile(r"\bp\s*=\s*(0?\.\d+|1(?:\.0+)?)\b", re.IGNORECASE)
_PAPER_OUTCOME_RE = re.compile(
    r"\b(?:primary outcome|dependent variable|main outcome)\s*(?:is|are|:)?\s*"
    r"([A-Za-z][A-Za-z0-9 _-]{2,40})",
    re.IGNORECASE,
)


class PreregRecord(BaseModel):
    """Normalized pre-analysis plan / registration record fetched from OSF or fixtures."""

    url: str
    title: str = ""
    description: str = ""
    target_n: int | None = None
    primary_outcomes: list[str] = Field(default_factory=list)


class DistributionMatchResult(BaseModel):
    """Two-sample KS and quantile alignment summary for subsampled simulation fallback."""

    n_reference: int
    n_subsampled: int
    ks_stat: float
    mean_diff: float
    quantile_max_diff: float
    matched: bool
    flags: list[str] = Field(default_factory=list)


class QuantitativeRobustnessResult(BaseModel):
    """Multiple-testing and outlier-leverage perturbation findings for Stage 5 dossier."""

    raw_p_values: list[float] = Field(default_factory=list)
    bonferroni_significant: int = 0
    bh_fdr_significant: int = 0
    raw_significant: int = 0
    winsorize_sign_flips: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


def _default_http_get(url: str) -> tuple[int, str]:
    with create_safe_http_client(timeout=10.0) as client:
        resp = client.get(url)
        return resp.status_code, resp.text


def fetch_preregistration_summary(
    url: str,
    *,
    http_get: Callable[[str], tuple[int, str]] | None = None,
) -> PreregRecord | None:
    """Fetch public OSF or web preregistration summary metadata when online checks are enabled."""
    fetcher = http_get or _default_http_get
    osf_match = _OSF_REG_URL_RE.search(url)
    target_url = f"https://api.osf.io/v2/registrations/{osf_match.group(1)}/" if osf_match else url
    try:
        status, body = fetcher(target_url)
    except Exception:
        return None
    if status != 200:
        return None

    title = ""
    desc = body[:4000]
    target_n: int | None = None
    outcomes: list[str] = []
    if target_url.startswith("https://api.osf.io"):
        try:
            payload = json.loads(body)
            attrs = payload.get("data", {}).get("attributes", {})
            title = str(attrs.get("title") or "")
            desc = str(attrs.get("description") or "")
        except json.JSONDecodeError:
            pass

    n_match = re.search(r"\b(?:target\s+)?N\s*=\s*(\d[\d,]*)", f"{title}\n{desc}", re.IGNORECASE)
    if n_match:
        target_n = int(n_match.group(1).replace(",", ""))
    for om in _PAPER_OUTCOME_RE.finditer(desc):
        val = om.group(1).strip().lower()
        if val and val not in outcomes:
            outcomes.append(val)

    return PreregRecord(
        url=url,
        title=title,
        description=desc,
        target_n=target_n,
        primary_outcomes=outcomes,
    )


def diff_preregistration_against_paper(
    record: PreregRecord,
    paper_text: str,
    claimed_ns: list[int],
) -> list[str]:
    """Compare planned registration N and primary outcomes against published manuscript claims."""
    flags: list[str] = []
    if record.target_n is not None and claimed_ns:
        target_n = record.target_n
        closest = min(claimed_ns, key=lambda n: abs(n - target_n))
        rel_diff = abs(closest - target_n) / max(target_n, 1)
        if rel_diff > 0.15:
            flags.append(
                f"PAP_SAMPLE_SIZE_DEVIATION: preregistered target N={target_n} vs "
                f"manuscript N={closest}"
            )
    paper_lower = paper_text.lower()
    for outcome in record.primary_outcomes:
        if outcome not in paper_lower:
            flags.append(
                f"PAP_OUTCOME_OMISSION: preregistered primary outcome '{outcome}' "
                "not found in paper"
            )
    return flags


def summarize_multiseed_runs(
    seed_runs: list[tuple[int, dict[str, float]]],
    *,
    rel_std_threshold: float = 0.05,
) -> list[str]:
    """Check stability and sign consistency across multi-seed sandbox re-executions."""
    if len(seed_runs) < 2:
        return []
    by_metric: dict[str, list[float]] = {}
    for _, metrics in seed_runs:
        for k, v in metrics.items():
            by_metric.setdefault(k, []).append(v)

    flags: list[str] = []
    for metric, vals in sorted(by_metric.items()):
        if len(vals) < 2:
            continue
        has_pos = any(v > 0 for v in vals)
        has_neg = any(v < 0 for v in vals)
        if has_pos and has_neg:
            flags.append(
                f"MULTISEED_SIGN_FLIP: metric '{metric}' flipped sign across seeds ({vals})"
            )
            continue
        mean_val = sum(vals) / len(vals)
        var = sum((v - mean_val) ** 2 for v in vals) / len(vals)
        std = math.sqrt(var)
        rel_std = std / max(abs(mean_val), 1e-12)
        if rel_std > rel_std_threshold:
            flags.append(
                f"MULTISEED_INSTABILITY: metric '{metric}' varied by rel_std={rel_std:.2%} "
                f"across {len(vals)} seeds"
            )
    return flags


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = min(len(sorted_vals) - 1, lo + 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def compare_distributions(
    reference_vals: list[float],
    subsampled_vals: list[float],
    *,
    ks_threshold: float = 0.25,
) -> DistributionMatchResult:
    """Compare reference and downscaled simulation output distributions via KS and quantiles."""
    a = sorted(reference_vals)
    b = sorted(subsampled_vals)
    if not a or not b:
        return DistributionMatchResult(
            n_reference=len(a),
            n_subsampled=len(b),
            ks_stat=1.0,
            mean_diff=0.0,
            quantile_max_diff=0.0,
            matched=False,
            flags=["DISTRIBUTION_FALLBACK_EMPTY"],
        )

    all_points = sorted(set(a + b))
    n_a, n_b = len(a), len(b)
    i = j = 0
    ks_stat = 0.0
    for x in all_points:
        while i < n_a and a[i] <= x:
            i += 1
        while j < n_b and b[j] <= x:
            j += 1
        ks_stat = max(ks_stat, abs((i / n_a) - (j / n_b)))

    mean_a = sum(a) / n_a
    mean_b = sum(b) / n_b
    mean_diff = abs(mean_a - mean_b)
    q_diffs = [abs(_quantile(a, q) - _quantile(b, q)) for q in (0.2, 0.4, 0.6, 0.8)]
    q_max = max(q_diffs)
    matched = ks_stat <= ks_threshold
    flags: list[str] = []
    if not matched:
        flags.append(
            f"SUBSAMPLED_DISTRIBUTION_MISMATCH: KS={ks_stat:.3f} > {ks_threshold:.2f}, "
            f"max_quantile_diff={q_max:.3f}"
        )

    return DistributionMatchResult(
        n_reference=n_a,
        n_subsampled=n_b,
        ks_stat=round(ks_stat, 4),
        mean_diff=round(mean_diff, 4),
        quantile_max_diff=round(q_max, 4),
        matched=matched,
        flags=flags,
    )


def _simple_ols_slope(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 5:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 1e-12:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / denom


def _csv_winsorize_sign_flip(path: Path) -> str | None:
    import csv

    try:
        with path.open("rt", encoding="utf-8", errors="replace", newline="") as fh:
            rows = list(csv.reader(fh))
    except OSError:
        return None
    if len(rows) < 12:
        return None

    headers = [h.strip().lower() for h in rows[0]]
    x_idx = next(
        (i for i, h in enumerate(headers) if h in {"treat", "treatment", "x", "policy"}),
        None,
    )
    y_idx = next(
        (i for i, h in enumerate(headers) if h in {"y", "outcome", "score", "effect", "wage"}),
        None,
    )
    if x_idx is None or y_idx is None:
        return None

    pairs: list[tuple[float, float]] = []
    for row in rows[1:]:
        if max(x_idx, y_idx) >= len(row):
            continue
        try:
            pairs.append((float(row[x_idx]), float(row[y_idx])))
        except ValueError:
            continue
    if len(pairs) < 10:
        return None

    raw_slope = _simple_ols_slope([p[0] for p in pairs], [p[1] for p in pairs])
    if raw_slope is None or abs(raw_slope) <= 1e-9:
        return None

    k = max(1, math.ceil(0.05 * len(pairs)))
    if len(pairs) - 2 * k < 6:
        return None
    sorted_by_y = sorted(pairs, key=lambda p: p[1])
    trimmed = sorted_by_y[k : len(sorted_by_y) - k]
    trimmed_slope = _simple_ols_slope([p[0] for p in trimmed], [p[1] for p in trimmed])
    if trimmed_slope is not None and (raw_slope * trimmed_slope < 0):
        return (
            f"OUTLIER_SENSITIVITY_FLIP: {path.name} slope flipped from {raw_slope:.4f} "
            f"to {trimmed_slope:.4f} after 5% tail trimming"
        )
    return None


def run_quantitative_perturbations(
    paper_text: str,
    data_profiles: list[DataFileProfile] | None = None,
    data_paths: list[Path] | None = None,
) -> QuantitativeRobustnessResult:
    """Apply Bonferroni/BH-FDR corrections to extracted p-values and 5%-trim OLS leverage check."""
    _ = data_profiles
    pvals = [
        float(m.group(1))
        for m in _PVAL_INLINE_RE.finditer(paper_text)
        if 0.0 <= float(m.group(1)) <= 1.0
    ]
    raw_sig = sum(1 for p in pvals if p < 0.05)
    m = len(pvals)
    bonf_sig = sum(1 for p in pvals if p < (0.05 / max(m, 1)))

    bh_sig = 0
    if m > 0:
        sorted_p = sorted(pvals)
        max_k = 0
        for idx, p in enumerate(sorted_p, start=1):
            if p <= (idx / m) * 0.05:
                max_k = idx
        bh_sig = max_k

    flips: list[str] = []
    for path in data_paths or []:
        if path.is_file() and path.suffix.lower() == ".csv":
            hit = _csv_winsorize_sign_flip(path)
            if hit:
                flips.append(hit)

    flags: list[str] = []
    if m >= 2 and raw_sig > bonf_sig:
        flags.append(
            f"BONFERRONI_SIGNIFICANCE_REVERSAL: {raw_sig}/{m} p<0.05 results drop to "
            f"{bonf_sig}/{m} under Bonferroni (alpha={0.05 / m:.4f})"
        )
    if m >= 2 and raw_sig > bh_sig:
        flags.append(
            f"BH_FDR_SIGNIFICANCE_REVERSAL: {raw_sig}/{m} p<0.05 results drop to "
            f"{bh_sig}/{m} under Benjamini-Hochberg FDR"
        )
    flags.extend(flips)

    return QuantitativeRobustnessResult(
        raw_p_values=pvals,
        bonferroni_significant=bonf_sig,
        bh_fdr_significant=bh_sig,
        raw_significant=raw_sig,
        winsorize_sign_flips=flips,
        flags=flags,
    )
