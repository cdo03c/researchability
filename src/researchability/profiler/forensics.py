"""Forensic statistical consistency checks (GRIM, statcheck-style p-value checks, N parsing)."""

from __future__ import annotations

import math
import re

from pydantic import BaseModel, Field

_SAMPLE_SIZE_PATTERNS = [
    re.compile(r"\b[Nn]\s*=\s*(\d{1,7}(?:,\d{3})*)\b"),
    re.compile(
        r"\b(\d{1,7}(?:,\d{3})*)\s+(?:samples|subjects|patients|participants|observations|examples|instances)\b",
        re.IGNORECASE,
    ),
]

_T_TEST_RE = re.compile(
    r"\bt\s*\(\s*(\d+(?:\.\d+)?)\s*\)\s*=\s*(-?\d+(?:\.\d+)?)\s*,\s*p\s*([<=>]+)\s*(\d*\.?\d+)",
    re.IGNORECASE,
)
_Z_TEST_RE = re.compile(
    r"\bz\s*=\s*(-?\d+(?:\.\d+)?)\s*,\s*p\s*([<=>]+)\s*(\d*\.?\d+)",
    re.IGNORECASE,
)
_CHI2_TEST_RE = re.compile(
    r"\b(?:chi2|χ2|x2)\s*\(\s*(\d+(?:\.\d+)?)\s*\)\s*=\s*(\d+(?:\.\d+)?)\s*,\s*p\s*([<=>]+)\s*(\d*\.?\d+)",
    re.IGNORECASE,
)
_REG_STAR_ENTRY_RE = re.compile(r"(-?\d+\.\d+)\s*(\*{1,3})\s*\(\s*(\d+\.\d+)\s*\)")
_REG_UNSTARRED_ENTRY_RE = re.compile(r"(?:^|\s)(-?\d+\.\d+)\s+\(\s*(\d+\.\d+)\s*\)(?!\s*\*)")
_LABELED_CI_RE = re.compile(
    r"\b(?:b|beta|OR|HR|RR|ATT|ATE|estimate|coef(?:ficient)?|effect)\s*=\s*(-?\d+\.\d+)\s*"
    r"(?!%)[,;(]*\s*(?:9[059]%\s*)?CIs?\s*[:=]?\s*"
    r"\[\s*(-?\d+\.\d+)\s*[,;\u2013-]+\s*(-?\d+\.\d+)\s*\]",
    re.IGNORECASE,
)
_CI_PAIR_RE = re.compile(
    r"\b(?:9[059]%\s*)?CIs?\s*[:=]?\s*\[\s*(-?\d+\.\d+)\s*[,;]+\s*(-?\d+\.\d+)\s*\]",
    re.IGNORECASE,
)
_COHEN_D_RE = re.compile(r"\bCohen(?:'s)?\s*d\s*=\s*(-?\d+(?:\.\d+)?)\b", re.IGNORECASE)
_PEARSON_R_RE = re.compile(r"\bPearson(?:'s)?\s*r\s*=\s*(-?\d+(?:\.\d+)?)\b", re.IGNORECASE)
_NEG_SPREAD_RE = re.compile(
    r"\b(?:SD|SE|standard\s+error|variance)\s*=\s*(-\d+(?:\.\d+)?)\b", re.IGNORECASE
)
_R2_RE = re.compile(r"(?:R\^2|R²)\s*=\s*(-?\d+(?:\.\d+)?)\b")
_BAD_P_RE = re.compile(r"\bp\s*=\s*(-?\d+\.\d+)\b", re.IGNORECASE)
_GRIM_MEAN_N_RE = re.compile(
    r"\b(?:M|mean)\s*=\s*(\d+\.\d{1,3})\s*[,;(]\s*[Nn]\s*=\s*(\d{1,3})\b",
    re.IGNORECASE,
)


class GrimResult(BaseModel):
    """Outcome of a Granularity-Related Inconsistency of Means (GRIM) test."""

    mean: float
    decimals: int
    sample_size: int
    consistent: bool
    reason: str = ""


class StatcheckResult(BaseModel):
    """Comparison between a reported test statistic and reported p-value."""

    raw_match: str
    test_type: str
    statistic: float
    df: float | None = None
    reported_operator: str
    reported_p: float
    computed_p: float
    consistent: bool
    decision_error: bool = False


class ForensicSummary(BaseModel):
    """Combined forensic numeric check results for a manuscript."""

    grim_results: list[GrimResult] = Field(default_factory=list)
    statcheck_results: list[StatcheckResult] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


def extract_claimed_sample_sizes(text: str) -> list[int]:
    """Extract candidate sample sizes (N) mentioned in paper text."""
    found: list[int] = []
    seen: set[int] = set()
    for pattern in _SAMPLE_SIZE_PATTERNS:
        for raw in pattern.findall(text):
            val = int(raw.replace(",", ""))
            if val > 0 and val not in seen:
                seen.add(val)
                found.append(val)
    return found


def check_grim(mean_str: str, sample_size: int) -> GrimResult:
    """Check whether an integer-scale sample mean is mathematically possible for N."""
    mean_val = float(mean_str)
    decimals = len(mean_str.split(".")[1]) if "." in mean_str else 0
    if sample_size <= 0:
        return GrimResult(
            mean=mean_val,
            decimals=decimals,
            sample_size=sample_size,
            consistent=False,
            reason="Sample size must be positive.",
        )
    if sample_size > 10**decimals:
        return GrimResult(
            mean=mean_val,
            decimals=decimals,
            sample_size=sample_size,
            consistent=True,
            reason="N exceeds granularity resolution (always consistent).",
        )

    product = mean_val * sample_size
    nearest = round(product)
    # Account for floating-point rounding +/- 0.5 * 10^(-decimals) * N
    max_diff = 0.5 * (10.0 ** (-decimals)) * sample_size + 1e-9
    consistent = abs(product - nearest) <= max_diff
    reason = (
        ""
        if consistent
        else (
            f"Mean {mean_str} * N={sample_size} = {product:.4f}, which cannot be produced "
            f"by integer scores (nearest integer sum is {nearest})."
        )
    )
    return GrimResult(
        mean=mean_val,
        decimals=decimals,
        sample_size=sample_size,
        consistent=consistent,
        reason=reason,
    )


def _betacf(a: float, b: float, x: float) -> float:
    """Evaluate continued fraction for incomplete beta function (Numerical Recipes)."""
    max_iter = 200
    eps = 3.0e-12
    fpmin = 1.0e-30
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b) for 0 <= x <= 1."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _gammaincc(a: float, x: float) -> float:
    """Upper regularized incomplete gamma Q(a, x) for chi-square survival."""
    if x <= 0.0:
        return 1.0
    if x < a + 1.0:
        # Series representation for P(a, x), return 1 - P
        ap = a
        total = 1.0 / a
        delta = total
        for _ in range(200):
            ap += 1.0
            delta *= x / ap
            total += delta
            if abs(delta) < abs(total) * 3e-12:
                break
        p_val = total * math.exp(-x + a * math.log(x) - math.lgamma(a))
        return max(0.0, 1.0 - p_val)
    # Continued fraction for Q(a, x)
    b = x + 1.0 - a
    c = 1.0 / 1e-30
    d = 1.0 / b
    h = d
    for i in range(1, 200):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < 1e-30:
            d = 1e-30
        c = b + an / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-12:
            break
    return max(0.0, math.exp(-x + a * math.log(x) - math.lgamma(a)) * h)


def two_tailed_t_p(t_stat: float, df: float) -> float:
    """Compute exact two-tailed Student-t p-value in pure Python."""
    x = df / (df + t_stat * t_stat)
    return _betainc(df / 2.0, 0.5, x)


def two_tailed_z_p(z_stat: float) -> float:
    """Compute exact two-tailed standard normal p-value."""
    return math.erfc(abs(z_stat) / math.sqrt(2.0))


def chi2_p(chi2_stat: float, df: float) -> float:
    """Compute exact upper-tail chi-square p-value."""
    return _gammaincc(df / 2.0, chi2_stat / 2.0)


def _evaluate_p_match(
    reported_op: str,
    reported_p: float,
    computed_p: float,
    tolerance: float = 0.015,
) -> tuple[bool, bool]:
    if reported_op == "=":
        consistent = abs(reported_p - computed_p) <= tolerance
    elif reported_op in {"<", "<="}:
        consistent = computed_p <= reported_p + tolerance
    elif reported_op in {">", ">="}:
        consistent = computed_p >= reported_p - tolerance
    else:
        consistent = True

    decision_error = (reported_p < 0.05 <= computed_p) or (computed_p < 0.05 <= reported_p)
    if reported_op == "<" and reported_p <= 0.05 and computed_p >= 0.05 + tolerance:
        decision_error = True
    return consistent, decision_error


def check_stat_consistency(text: str) -> list[StatcheckResult]:
    """Scan manuscript text for t-, z-, and chi-square statistics and verify p-values."""
    results: list[StatcheckResult] = []

    for match in _T_TEST_RE.finditer(text):
        df = float(match.group(1))
        stat = float(match.group(2))
        op = match.group(3)
        rep_p = float(match.group(4))
        if df <= 0:
            continue
        comp_p = two_tailed_t_p(stat, df)
        consistent, dec_err = _evaluate_p_match(op, rep_p, comp_p)
        results.append(
            StatcheckResult(
                raw_match=match.group(0),
                test_type="t",
                statistic=stat,
                df=df,
                reported_operator=op,
                reported_p=rep_p,
                computed_p=comp_p,
                consistent=consistent,
                decision_error=dec_err,
            )
        )

    for match in _Z_TEST_RE.finditer(text):
        stat = float(match.group(1))
        op = match.group(2)
        rep_p = float(match.group(3))
        comp_p = two_tailed_z_p(stat)
        consistent, dec_err = _evaluate_p_match(op, rep_p, comp_p)
        results.append(
            StatcheckResult(
                raw_match=match.group(0),
                test_type="z",
                statistic=stat,
                reported_operator=op,
                reported_p=rep_p,
                computed_p=comp_p,
                consistent=consistent,
                decision_error=dec_err,
            )
        )

    for match in _CHI2_TEST_RE.finditer(text):
        df = float(match.group(1))
        stat = float(match.group(2))
        op = match.group(3)
        rep_p = float(match.group(4))
        if df <= 0:
            continue
        comp_p = chi2_p(stat, df)
        consistent, dec_err = _evaluate_p_match(op, rep_p, comp_p)
        results.append(
            StatcheckResult(
                raw_match=match.group(0),
                test_type="chi2",
                statistic=stat,
                df=df,
                reported_operator=op,
                reported_p=rep_p,
                computed_p=comp_p,
                consistent=consistent,
                decision_error=dec_err,
            )
        )

    return results


def check_table_stars_and_cis(text: str) -> list[str]:
    """Audit regression table significance stars vs. coef/SE ratios and CI bounds."""
    flags: list[str] = []
    star_matches = list(_REG_STAR_ENTRY_RE.finditer(text))
    for match in star_matches:
        coef = float(match.group(1))
        stars = match.group(2)
        se = float(match.group(3))
        if se <= 0:
            continue
        p_val = two_tailed_z_p(abs(coef) / se)
        if len(stars) == 3 and p_val > 0.06:
            flags.append(
                f"Significance star mismatch on '{match.group(0)}' (implied p={p_val:.4f} > 0.05)"
            )
        elif len(stars) == 2 and p_val > 0.10:
            flags.append(
                f"Significance star mismatch on '{match.group(0)}' (implied p={p_val:.4f} > 0.10)"
            )
        elif len(stars) == 1 and p_val > 0.15:
            flags.append(
                f"Significance star mismatch on '{match.group(0)}' (implied p={p_val:.4f} > 0.15)"
            )

    if len(star_matches) >= 2:
        # Restrict unstarred table checks to lines near starred table rows to avoid prose hits
        lines = text.splitlines()
        starred_lines: set[int] = set()
        for idx, line_text in enumerate(lines):
            if _REG_STAR_ENTRY_RE.search(line_text):
                for nearby in range(max(0, idx - 5), min(len(lines), idx + 6)):
                    starred_lines.add(nearby)
        for line_idx in sorted(starred_lines):
            for match in _REG_UNSTARRED_ENTRY_RE.finditer(lines[line_idx]):
                coef = float(match.group(1))
                se = float(match.group(2))
                if se <= 0:
                    continue
                p_val = two_tailed_z_p(abs(coef) / se)
                if p_val < 0.0005:
                    flags.append(
                        f"Missing significance stars on '{match.group(0).strip()}' "
                        f"(implied p={p_val:.5f} < 0.001 in starred table)"
                    )

    for match in _CI_PAIR_RE.finditer(text):
        lo = float(match.group(1))
        hi = float(match.group(2))
        if lo > hi:
            flags.append(f"Inverted confidence interval bounds in '{match.group(0)}' ({lo} > {hi})")

    for match in _LABELED_CI_RE.finditer(text):
        est = float(match.group(1))
        lo = float(match.group(2))
        hi = float(match.group(3))
        if lo <= hi and not (lo - 1e-6 <= est <= hi + 1e-6):
            flags.append(
                f"Confidence interval mismatch on '{match.group(0)}': "
                f"estimate {est} outside [{lo}, {hi}]"
            )

    return flags


def check_effect_size_bounds(text: str) -> list[str]:
    """Flag implausible effect sizes, out-of-range correlations/R^2, or negative variances."""
    flags: list[str] = []
    for match in _COHEN_D_RE.finditer(text):
        d_val = float(match.group(1))
        if abs(d_val) > 3.0:
            flags.append(
                f"Implausibly large standardized effect size '{match.group(0)}' (|d| > 3.0)"
            )
    for match in _PEARSON_R_RE.finditer(text):
        r_val = float(match.group(1))
        if abs(r_val) > 1.0:
            flags.append(f"Out-of-range correlation '{match.group(0)}' (|r| > 1.0)")
    for match in _NEG_SPREAD_RE.finditer(text):
        flags.append(f"Negative spread/variance reported in '{match.group(0)}'")
    for match in _R2_RE.finditer(text):
        r2_val = float(match.group(1))
        if r2_val < 0.0 or r2_val > 1.0:
            flags.append(f"Out-of-range R^2 in '{match.group(0)}'")
    for match in _BAD_P_RE.finditer(text):
        p_val = float(match.group(1))
        if p_val < 0.0 or p_val > 1.0:
            flags.append(f"Impossible p-value outside [0, 1] in '{match.group(0)}'")
    return flags


def extract_paired_grim_checks(text: str) -> list[GrimResult]:
    """Run GRIM checks on explicit `mean = X.XX, N = YY` pairs within granularity bounds."""
    results: list[GrimResult] = []
    for match in _GRIM_MEAN_N_RE.finditer(text):
        mean_str = match.group(1)
        n_val = int(match.group(2))
        decimals = len(mean_str.split(".")[1])
        if 1 <= n_val <= 10**decimals:
            results.append(check_grim(mean_str, n_val))
    return results


def run_forensic_checks(text: str) -> ForensicSummary:
    """Run automated numeric consistency checks on paper text."""
    stat_results = check_stat_consistency(text)
    grim_results = extract_paired_grim_checks(text)
    flags: list[str] = []
    for item in stat_results:
        if not item.consistent:
            kind = "decision error" if item.decision_error else "p-value mismatch"
            flags.append(
                f"Statistical {kind} in '{item.raw_match}' (computed p={item.computed_p:.4f})"
            )
    for grim in grim_results:
        if not grim.consistent and grim.reason:
            flags.append(f"GRIM inconsistency: {grim.reason}")
    flags.extend(check_table_stars_and_cis(text))
    flags.extend(check_effect_size_bounds(text))
    return ForensicSummary(
        grim_results=grim_results,
        statcheck_results=stat_results,
        flags=flags,
    )
