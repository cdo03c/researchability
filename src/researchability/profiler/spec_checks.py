"""I4R-derived static lineage, GIS sidecars, table coverage, script hygiene, and spec audits."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from researchability.profiler.hygiene import _SCRIPT_READ_PATTERNS, _SCRIPT_WRITE_PATTERNS

if TYPE_CHECKING:
    from researchability.profiler.data_profiler import DataFileProfile

_PROCESSED_DATA_RE = re.compile(
    r"(?:clean(?:ed)?|processed|final|analytic|analysis_data|merged|panel|wrk)",
    re.IGNORECASE,
)
_PREP_SCRIPT_RE = re.compile(
    r"(?:prep|preprocess|clean|etl|wrangle|build|construct|ingest)",
    re.IGNORECASE,
)
_PREP_TRANSFORM_RE = re.compile(
    r"(?:to_csv|write_csv|write\.csv|saveRDS|save\b|export\s+delimited|"
    r"\bmerge\b|\bjoin\b|\bmvdecode\b|\brecode\b)",
    re.IGNORECASE,
)
_RESTRICTED_RAW_RE = re.compile(
    r"(?:raw data.{0,40}(?:restricted|confidential|proprietary|not public|irb|dua)|"
    r"proprietary.{0,30}raw|confidential microdata)",
    re.IGNORECASE,
)
_SPATIAL_BINARY_REF_RE = re.compile(
    r"""["']([A-Za-z0-9_./-]+\.(?:shp|geojson|gpkg|tif|tiff|rds|rdata|dta))["']""",
    re.IGNORECASE,
)
_PAPER_TABLE_FIG_RE = re.compile(
    r"\b(Table|Figure)\s+(S?\d{1,2}|[A-Z]\d{1,2})\b",
    re.IGNORECASE,
)
_LOG1P_VAR_RE = re.compile(
    r"\b(?:log1p|np\.log1p|ln|log)\s*\(\s*(?:1\s*\+\s*)?([A-Za-z][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_ASSIGN_MODEL_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_]{0,20})\s*(?:<-|=)\s*"
    r"(?:feols|lm|glm|felm|OLS|WLS|sm\.OLS|smf\.ols|reghdfe|reg)\b",
    re.MULTILINE,
)
_FUNC_CALL_LINE_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]*\s*\(([^()\n]{20,})\)")
_STATA_VERSION_RE = re.compile(r"^\s*version\s+\d+", re.IGNORECASE | re.MULTILINE)
_STATA_VARABBREV_RE = re.compile(r"\bset\s+varabbrev\s+off\b", re.IGNORECASE)
_STATA_SORT_RE = re.compile(r"^\s*sort\s+([^\n]+)$", re.IGNORECASE | re.MULTILINE)
_SOURCE_OR_DO_RE = re.compile(r"""(?:\bsource\s*\(|\bdo\s+["'\w])""", re.IGNORECASE)
_ANY_ANALYSIS_RE = re.compile(
    r"\b(?:feols|lm|glm|OLS|reghdfe|reg|regress|ggplot|matplotlib|plt\.)\b",
    re.IGNORECASE,
)

_MAGIC_VEC_RE = re.compile(
    r"(?:c\s*\(\s*-?\d+\.\d+(?:\s*,\s*-?\d+\.\d+){2,}\s*\)|"
    r"\[\s*-?\d+\.\d+(?:\s*,\s*-?\d+\.\d+){2,}\s*\])"
)
_MAGIC_RESCALE_RE = re.compile(
    r"\b(?:r2|r_sq|coef|beta|est|val|metric)\w*\s*[/*]\s*(?!0?\.0+\b|10+\b)(0\.\d{2,}|[2-9]\.\d+)\b",
    re.IGNORECASE,
)
_COMMENTED_DIAG_RE = re.compile(
    r"^\s*(?:#|//|\*)\s*.*?\b(?:filter|subset|drop\s+if|dropna|"
    r"balance|t\.test|ttest|vif|ip_addr|exclude)\b",
    re.IGNORECASE | re.MULTILINE,
)
_COL_INDEX_RE = re.compile(
    r"""(?:\bdf\s*\[\s*["']([A-Za-z][A-Za-z0-9_]*)["']\s*\]|"""
    r"""\b(?:df|dat|data)\$([A-Za-z][A-Za-z0-9_]*)\b)"""
)
_ASSIGN_COL_RE = re.compile(
    r"""(?:df\s*\[\s*["']([A-Za-z][A-Za-z0-9_]*)["']\s*\]\s*=|"""
    r"""\b(?:df|dat|data)\$([A-Za-z][A-Za-z0-9_]*)\s*<-)"""
)

_REG_CALL_LINE_RE = re.compile(
    r"^\s*(?:.*?(?:<-|=)\s*)?"
    r"(?:feols|lm|glm|svyglm|felm|OLS|WLS|sm\.OLS|smf\.ols|reghdfe|regress|reg)\b([^\n]*)",
    re.IGNORECASE | re.MULTILINE,
)
_YEAR_RANGE_RE = re.compile(r"\b(19\d{2}|20\d{2})\s*[-\u2013to]+\s*(19\d{2}|20\d{2})\b")
_YEAR_FILTER_RE = re.compile(r"\byear\s*<=\s*(19\d{2}|20\d{2})\b", re.IGNORECASE)


class DataLineageAudit(BaseModel):
    """Directed flow audit checking raw inputs -> prep scripts -> cleaned outputs -> analysis."""

    prep_scripts: list[str] = Field(default_factory=list)
    analysis_scripts: list[str] = Field(default_factory=list)
    raw_inputs: list[str] = Field(default_factory=list)
    processed_inputs: list[str] = Field(default_factory=list)
    written_intermediates: list[str] = Field(default_factory=list)
    only_processed_without_prep: bool = False
    flags: list[str] = Field(default_factory=list)


class TableFigureCoverageResult(BaseModel):
    """Coverage comparison between paper Table/Figure numbers and script outputs."""

    claimed_items: list[str] = Field(default_factory=list)
    matched_items: list[str] = Field(default_factory=list)
    uncovered_items: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


class SpecificationAuditResult(BaseModel):
    """Cross-check of regression weights, clustering, split rules, and date windows."""

    regression_calls: int = 0
    inconsistent_weights: bool = False
    inconsistent_clustering: bool = False
    mismatches: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


def _extract_matches(patterns: list[re.Pattern[str]], text: str) -> list[str]:
    found: list[str] = []
    for pat in patterns:
        for m in pat.finditer(text):
            val = m.group(1).strip()
            if val and val not in found:
                found.append(val)
    return found


def audit_data_lineage(
    code_texts: dict[str, str],
    data_files: list[str],
    readme_texts: dict[str, str] | None = None,
    paper_text: str = "",
) -> DataLineageAudit:
    """Inspect whether analysis scripts trace back to raw inputs via cleaning scripts."""
    prep_scripts: list[str] = []
    analysis_scripts: list[str] = []
    raw_inputs: list[str] = []
    processed_inputs: list[str] = []
    written_intermediates: list[str] = []

    for rel, body in code_texts.items():
        reads = _extract_matches(_SCRIPT_READ_PATTERNS, body)
        writes = _extract_matches(_SCRIPT_WRITE_PATTERNS, body)
        is_prep = bool(_PREP_SCRIPT_RE.search(rel)) or any(
            w.lower().endswith((".csv", ".rds", ".dta", ".parquet")) for w in writes
        )
        if is_prep:
            prep_scripts.append(rel)
        if _ANY_ANALYSIS_RE.search(body) or not is_prep:
            analysis_scripts.append(rel)

        for r in reads:
            if _PROCESSED_DATA_RE.search(r):
                if r not in processed_inputs:
                    processed_inputs.append(r)
            elif r not in raw_inputs:
                raw_inputs.append(r)
        for w in writes:
            if w.lower().endswith((".csv", ".tsv", ".rds", ".dta", ".parquet", ".rdata")):
                written_intermediates.append(w)

    if not processed_inputs:
        for df in data_files:
            if _PROCESSED_DATA_RE.search(df):
                processed_inputs.append(df)

    disclosure_corpus = "\n".join([*(readme_texts or {}).values(), paper_text])
    has_restricted_note = bool(_RESTRICTED_RAW_RE.search(disclosure_corpus))
    only_processed = (
        bool(processed_inputs)
        and not prep_scripts
        and not written_intermediates
        and not any(_PREP_TRANSFORM_RE.search(b) for b in code_texts.values())
        and not has_restricted_note
    )

    flags: list[str] = []
    if only_processed:
        proc_str = ", ".join(processed_inputs[:3])
        flags.append(
            "PROCESSED_DATA_ONLY_NO_RAW_LINEAGE: scripts consume cleaned/processed dataset(s) "
            f"({proc_str}) without upstream cleaning script or raw disclosure"
        )

    return DataLineageAudit(
        prep_scripts=prep_scripts,
        analysis_scripts=analysis_scripts,
        raw_inputs=raw_inputs,
        processed_inputs=processed_inputs,
        written_intermediates=written_intermediates,
        only_processed_without_prep=only_processed,
        flags=flags,
    )


def audit_gis_and_binary_sidecars(
    workdir: Path | None,
    code_texts: dict[str, str],
    all_known_files: list[str],
) -> list[str]:
    """Check shapefile sidecar completeness (.dbf/.shx/.prj) and missing spatial/binary assets."""
    flags: list[str] = []
    normalized_known = {f.replace("\\", "/").lstrip("./").lower() for f in all_known_files}
    known_basenames = {Path(f).name.lower() for f in all_known_files}

    # 1. .shp sidecar completeness (.dbf, .shx, .prj)
    for rel in all_known_files:
        if not rel.lower().endswith(".shp"):
            continue
        stem = rel[:-4].lower()
        missing_exts: list[str] = []
        for ext in (".dbf", ".shx", ".prj"):
            candidate_rel = f"{stem}{ext}"
            exists_in_list = candidate_rel in normalized_known
            exists_on_disk = workdir is not None and (workdir / f"{rel[:-4]}{ext}").is_file()
            if not exists_in_list and not exists_on_disk:
                missing_exts.append(ext)
        if missing_exts:
            flags.append(
                f"MISSING_GIS_SIDECARS: {rel} missing required shapefile sidecar(s): "
                f"{', '.join(missing_exts)}"
            )

    # 2. References in code to .shp/.geojson/.gpkg/.tif/.rds/.rdata/.dta missing from package
    written_targets = {
        Path(w).name.lower()
        for body in code_texts.values()
        for w in _extract_matches(_SCRIPT_WRITE_PATTERNS, body)
    }
    for rel_script, body in code_texts.items():
        for match in _SPATIAL_BINARY_REF_RE.finditer(body):
            target = match.group(1).replace("\\", "/").lstrip("./")
            base = Path(target).name.lower()
            if (
                target.lower() in normalized_known
                or base in known_basenames
                or base in written_targets
            ):
                continue
            if workdir is not None and (workdir / target).exists():
                continue
            flags.append(f"MISSING_SPATIAL_OR_BINARY_ASSET: {rel_script} -> {target}")

    return list(dict.fromkeys(flags))


def audit_table_figure_coverage(
    paper_text: str,
    code_texts: dict[str, str],
    artifact_files: list[str] | None = None,
) -> TableFigureCoverageResult:
    """Compare claimed Table/Figure numbers against generating code and output filenames."""
    if not code_texts:
        return TableFigureCoverageResult()

    claimed: list[str] = []
    seen_claimed: set[str] = set()
    for match in _PAPER_TABLE_FIG_RE.finditer(paper_text):
        kind = match.group(1).lower()
        num = match.group(2).lower()
        # Filter obvious false positives like Table 2020 or large arbitrary indices
        digits = re.sub(r"[^0-9]", "", num)
        if digits and int(digits) > 40:
            continue
        label = f"{kind}_{num}"
        if label not in seen_claimed:
            seen_claimed.add(label)
            claimed.append(label)

    if not claimed:
        return TableFigureCoverageResult()

    search_corpus = "\n".join([*code_texts.keys(), *code_texts.values(), *(artifact_files or [])])
    corpus_lower = search_corpus.lower()
    matched: list[str] = []
    uncovered: list[str] = []

    for item in claimed:
        kind, num = item.split("_", maxsplit=1)
        short = "tab" if kind == "table" else "fig"
        pat = re.compile(
            rf"(?:{kind}|{short})\s*[_.-]?\s*0*{re.escape(num)}\b",
            re.IGNORECASE,
        )
        if pat.search(corpus_lower):
            matched.append(item)
        else:
            uncovered.append(item)

    flags: list[str] = []
    if uncovered:
        flags.append(
            f"UNCOVERED_PAPER_TABLE_OR_FIGURE: no code/output match for {', '.join(uncovered[:8])}"
        )

    return TableFigureCoverageResult(
        claimed_items=claimed,
        matched_items=matched,
        uncovered_items=uncovered,
        flags=flags,
    )


def audit_script_isolation_and_stata(
    code_texts: dict[str, str],
    data_profiles: list[DataFileProfile] | None = None,
) -> list[str]:
    """Check cross-script global workspace reliance, Stata hygiene, overwrites, and log(1+y)."""
    flags: list[str] = []
    r_stata_scripts = [p for p in code_texts if Path(p).suffix.lower() in {".r", ".rmd", ".do"}]
    non_entry = [
        p for p in sorted(r_stata_scripts) if not _PREP_SCRIPT_RE.search(p) and "master" not in p
    ]

    if len(r_stata_scripts) >= 2:
        for rel in non_entry:
            body = code_texts[rel]
            has_read = bool(_extract_matches(_SCRIPT_READ_PATTERNS, body))
            has_source = bool(_SOURCE_OR_DO_RE.search(body))
            if _ANY_ANALYSIS_RE.search(body) and not has_read and not has_source:
                flags.append(
                    f"IMPLICIT_GLOBAL_STATE: {rel} runs analysis without reading files or source/do"
                )

    zero_mass_map: dict[str, float] = {}
    for prof in data_profiles or []:
        for col, frac in prof.zero_mass_columns.items():
            zero_mass_map[col.lower()] = max(zero_mass_map.get(col.lower(), 0.0), frac)

    for rel, body in code_texts.items():
        ext = Path(rel).suffix.lower()
        if ext == ".do":
            missing_stata: list[str] = []
            if not _STATA_VERSION_RE.search(body):
                missing_stata.append("version")
            if not _STATA_VARABBREV_RE.search(body):
                missing_stata.append("set varabbrev off")
            for sm in _STATA_SORT_RE.finditer(body):
                if ", stable" not in sm.group(1).lower() and ",stable" not in sm.group(1).lower():
                    missing_stata.append("sort ..., stable")
                    break
            if missing_stata:
                flags.append(f"STATA_HYGIENE_RISK: {rel} missing {', '.join(missing_stata)}")

        # In-script model variable overwrite before saving/summarizing
        assignments = list(_ASSIGN_MODEL_RE.finditer(body))
        seen_vars: dict[str, int] = {}
        for am in assignments:
            var = am.group(1)
            prev_pos = seen_vars.get(var)
            if prev_pos is not None:
                between = body[prev_pos : am.start()]
                if not re.search(
                    rf"\b(?:summary|stargazer|etable|esttab|save|write)\b[^\n]*\b{re.escape(var)}\b",
                    between,
                    re.IGNORECASE,
                ):
                    flags.append(
                        f"MODEL_OBJECT_OVERWRITE: {rel} reassigns '{var}' before exporting/saving"
                    )
                    break
            seen_vars[var] = am.end()

        # Excessive positional arguments (> 10 positional values in a single call)
        for call_m in _FUNC_CALL_LINE_RE.finditer(body):
            arg_str = call_m.group(1)
            parts = [p.strip() for p in arg_str.split(",") if p.strip()]
            positional = [p for p in parts if "=" not in p]
            if len(positional) > 10:
                flags.append(
                    f"EXCESSIVE_POSITIONAL_ARGS: {rel} passes {len(positional)} positional args"
                )
                break

        # ln(1 + y) / log1p(y) on >30% zero-mass column
        if zero_mass_map:
            for lm in _LOG1P_VAR_RE.finditer(body):
                col = lm.group(1).lower()
                if col in zero_mass_map and zero_mass_map[col] > 0.30:
                    pct = zero_mass_map[col] * 100.0
                    flags.append(
                        f"LOG1P_HIGH_ZERO_MASS: {rel} transforms '{lm.group(1)}' with "
                        f"log/ln(1+y) despite {pct:.0f}% zeros"
                    )
                    break

    return list(dict.fromkeys(flags))


def audit_constants_and_diagnostics(
    code_texts: dict[str, str],
    known_columns: set[str] | None = None,
) -> list[str]:
    """Detect magic constants, commented-out exclusion/balance checks, and missing columns."""
    flags: list[str] = []
    cols_lower = {c.lower() for c in (known_columns or set()) if c}

    for rel, body in code_texts.items():
        if _MAGIC_VEC_RE.search(body) or _MAGIC_RESCALE_RE.search(body):
            flags.append(
                f"HARDCODED_MAGIC_CONSTANT: {rel} contains manual numeric literal/rescaling vector"
            )
        if _COMMENTED_DIAG_RE.search(body):
            flags.append(
                f"COMMENTED_OUT_DIAGNOSTIC_CHECK: {rel} contains commented-out filter/diagnostic"
            )
        if cols_lower:
            assigned = {
                (m.group(1) or m.group(2)).lower()
                for m in _ASSIGN_COL_RE.finditer(body)
                if m.group(1) or m.group(2)
            }
            for cm in _COL_INDEX_RE.finditer(body):
                col = (cm.group(1) or cm.group(2) or "").strip()
                if col and col.lower() not in cols_lower and col.lower() not in assigned:
                    flags.append(
                        f"MISSING_REFERENCED_COLUMN: {rel} references absent column '{col}'"
                    )
                    break

    return list(dict.fromkeys(flags))


def audit_model_specifications(
    paper_text: str,
    code_texts: dict[str, str],
) -> SpecificationAuditResult:
    """Compare regression weights, SE clustering, splits, and date windows across code & paper."""
    total_calls = 0
    inconsistent_weights = False
    inconsistent_clustering = False
    mismatches: list[str] = []
    flags: list[str] = []

    weight_pat = re.compile(r"(?:weights\s*=|\[(?:aw|pw|fw)\s*=|\bsvy\b)", re.IGNORECASE)
    cluster_pat = re.compile(r"(?:cluster\s*=|vcov\s*=|vce\s*\(\s*cluster)", re.IGNORECASE)

    combined_code = "\n".join(code_texts.values())
    for rel, body in code_texts.items():
        calls = [m.group(0) for m in _REG_CALL_LINE_RE.finditer(body)]
        total_calls += len(calls)
        if len(calls) >= 2:
            has_w = [bool(weight_pat.search(c)) for c in calls]
            if any(has_w) and not all(has_w):
                inconsistent_weights = True
                msg = f"INCONSISTENT_WEIGHTS_OR_SE: {rel} mixes weighted and unweighted regressions"
                flags.append(msg)
                mismatches.append(msg)
            has_c = [bool(cluster_pat.search(c)) for c in calls]
            if any(has_c) and not all(has_c):
                inconsistent_clustering = True
                msg = f"INCONSISTENT_WEIGHTS_OR_SE: {rel} mixes clustered and non-clustered SEs"
                flags.append(msg)
                mismatches.append(msg)

    # Check median vs. mean split discrepancies
    paper_median = bool(re.search(r"\bmedian\s+split\b", paper_text, re.IGNORECASE))
    code_mean = bool(
        re.search(r"(?:mean|np\.mean)\s*\([^)]+\)\s*(?:,|;|\n|>=|>|<|<=)", combined_code)
    )
    code_median = bool(re.search(r"\bmedian\s*\(", combined_code, re.IGNORECASE))
    if paper_median and code_mean and not code_median:
        msg = "SPECIFICATION_MISMATCH: manuscript claims median split while code uses mean()"
        mismatches.append(msg)
        flags.append(msg)

    # Check claimed sample end-year vs explicit code year filter (e.g. 2013-2019 vs year <= 2018)
    paper_end_years = [int(m.group(2)) for m in _YEAR_RANGE_RE.finditer(paper_text)]
    code_max_years = [int(m.group(1)) for m in _YEAR_FILTER_RE.finditer(combined_code)]
    if paper_end_years and code_max_years:
        p_max = max(paper_end_years)
        c_max = max(code_max_years)
        if not math.isclose(p_max, c_max, abs_tol=0.0):
            msg = (
                f"SPECIFICATION_MISMATCH: paper cites sample through {p_max} "
                f"but code filters year <= {c_max}"
            )
            mismatches.append(msg)
            flags.append(msg)

    return SpecificationAuditResult(
        regression_calls=total_calls,
        inconsistent_weights=inconsistent_weights,
        inconsistent_clustering=inconsistent_clustering,
        mismatches=list(dict.fromkeys(mismatches)),
        flags=list(dict.fromkeys(flags)),
    )
