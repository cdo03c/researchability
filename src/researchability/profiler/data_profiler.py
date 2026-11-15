"""Deterministic schema and row-count profiler for common research data formats."""

from __future__ import annotations

import csv
import gzip
import json
import re
import struct
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_MISSING_TOKENS = {"", "na", "n/a", "nan", "null", "none", "."}
_STRONG_SENTINELS = {-9999, -999, -99, -98, -88, 9999}
_SMALL_NEG_SENTINELS = {-1, -2, -3, -8, -9}
_PII_HEADER_RE = re.compile(
    r"^(?:e?_?mail|email_address|ip|ip_addr(?:ess)?|phone|telephone|mobile|ssn|"
    r"social_security|full_?name|first_?name|last_?name|home_address|"
    r"latitude|longitude|geo_?lat|geo_?lon)$",
    re.IGNORECASE,
)
_EMAIL_VAL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IPV4_VAL_RE = re.compile(r"^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$")


class DataFileProfile(BaseModel):
    """Schema, dimension, and numeric summary for one data file."""

    path: str
    format: str
    parseable: bool
    row_count: int | None = None
    column_count: int | None = None
    columns: list[str] = Field(default_factory=list)
    numeric_means: dict[str, float] = Field(default_factory=dict)
    zero_mass_columns: dict[str, float] = Field(default_factory=dict)
    sentinel_columns: dict[str, list[str]] = Field(default_factory=dict)
    empty_columns: list[str] = Field(default_factory=list)
    pii_findings: list[str] = Field(default_factory=list)
    parse_error: str | None = None


def _analyze_tab_pii_and_sentinels(
    columns: list[str],
    row_count: int,
    non_missing_counts: list[int],
    numeric_distinct: list[set[float]],
    strong_hits: list[set[int]],
    small_neg_hits: list[set[int]],
    email_hits: list[str],
    ip_hits: list[str],
) -> tuple[dict[str, list[str]], list[str], list[str]]:
    sentinels: dict[str, list[str]] = {}
    empty_cols: list[str] = []
    pii_findings: list[str] = []

    for idx, col in enumerate(columns):
        col_name = col or f"col_{idx}"
        if row_count >= 5 and non_missing_counts[idx] == 0:
            empty_cols.append(col_name)
        if _PII_HEADER_RE.match(col_name):
            pii_findings.append(f"Suspicious PII column header '{col_name}'")

        vals = numeric_distinct[idx]
        if len(vals) >= 2:
            col_reasons: list[str] = []
            if strong_hits[idx]:
                codes = ", ".join(str(c) for c in sorted(strong_hits[idx]))
                col_reasons.append(f"strong sentinel code(s) [{codes}] mixed with data values")
            non_neg_max = max(vals)
            has_pos_only_neg_sentinels = (
                bool(small_neg_hits[idx])
                and non_neg_max >= 20.0
                and all(v >= 0 or (v.is_integer() and int(v) in _SMALL_NEG_SENTINELS) for v in vals)
            )
            if has_pos_only_neg_sentinels:
                codes = ", ".join(str(c) for c in sorted(small_neg_hits[idx]))
                col_reasons.append(
                    f"negative survey missing code(s) [{codes}] mixed with positive range "
                    f"(max={non_neg_max:g})"
                )
            if col_reasons:
                sentinels[col_name] = col_reasons

    if email_hits:
        pii_findings.append(
            f"Email value(s) detected in column(s): {', '.join(sorted(set(email_hits)))}"
        )
    if ip_hits:
        pii_findings.append(
            f"IPv4 value(s) detected in column(s): {', '.join(sorted(set(ip_hits)))}"
        )

    return sentinels, empty_cols, pii_findings


def _profile_delimited(path: Path, delimiter: str, compressed: bool) -> DataFileProfile:
    opener = gzip.open if compressed else open
    base_fmt = "tsv" if delimiter == "\t" else "csv"
    fmt = f"{base_fmt}.gz" if compressed else base_fmt
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.reader(fh, delimiter=delimiter)
            header = next(reader, None)
            if header is None:
                return DataFileProfile(
                    path=str(path),
                    format=fmt,
                    parseable=False,
                    parse_error="Empty delimited file",
                )
            columns = [col.strip() for col in header]
            n_cols = len(columns)
            row_count = 0
            sums = [0.0] * n_cols
            counts = [0] * n_cols
            zero_counts = [0] * n_cols
            non_missing = [0] * n_cols
            numeric_distinct: list[set[float]] = [set() for _ in range(n_cols)]
            strong_hits: list[set[int]] = [set() for _ in range(n_cols)]
            small_neg_hits: list[set[int]] = [set() for _ in range(n_cols)]
            email_cols: list[str] = []
            ip_cols: list[str] = []

            for row in reader:
                if not row:
                    continue
                row_count += 1
                for idx in range(min(len(row), n_cols)):
                    val_str = row[idx].strip()
                    if val_str.lower() in _MISSING_TOKENS:
                        continue
                    non_missing[idx] += 1
                    col_label = columns[idx] or f"col_{idx}"
                    if row_count <= 500:
                        if "@" in val_str and _EMAIL_VAL_RE.search(val_str):
                            email_cols.append(col_label)
                        elif _IPV4_VAL_RE.match(val_str):
                            ip_cols.append(col_label)
                    try:
                        fval = float(val_str)
                        sums[idx] += fval
                        counts[idx] += 1
                        if fval == 0.0:
                            zero_counts[idx] += 1
                        if len(numeric_distinct[idx]) < 200:
                            numeric_distinct[idx].add(fval)
                        if fval.is_integer():
                            ival = int(fval)
                            if ival in _STRONG_SENTINELS:
                                strong_hits[idx].add(ival)
                            elif ival in _SMALL_NEG_SENTINELS:
                                small_neg_hits[idx].add(ival)
                    except ValueError:
                        continue
            means = {
                columns[i]: sums[i] / counts[i]
                for i in range(n_cols)
                if counts[i] > 0 and columns[i]
            }
            zero_mass = {
                columns[i]: round(zero_counts[i] / counts[i], 4)
                for i in range(n_cols)
                if counts[i] >= 5 and zero_counts[i] > 0 and columns[i]
            }
            sentinel_cols, empty_cols, pii_findings = _analyze_tab_pii_and_sentinels(
                columns,
                row_count,
                non_missing,
                numeric_distinct,
                strong_hits,
                small_neg_hits,
                email_cols,
                ip_cols,
            )
            return DataFileProfile(
                path=str(path),
                format=fmt,
                parseable=True,
                row_count=row_count,
                column_count=n_cols,
                columns=columns,
                numeric_means=means,
                zero_mass_columns=zero_mass,
                sentinel_columns=sentinel_cols,
                empty_columns=empty_cols,
                pii_findings=pii_findings,
            )
    except Exception as exc:
        return DataFileProfile(
            path=str(path),
            format=fmt,
            parseable=False,
            parse_error=str(exc),
        )


def _scan_json_records_for_pii(records: list[Any], cols: list[str]) -> list[str]:
    findings: list[str] = []
    for c in cols:
        if _PII_HEADER_RE.match(c):
            findings.append(f"Suspicious PII column header '{c}'")
    email_cols: set[str] = set()
    ip_cols: set[str] = set()
    for rec in records[:200]:
        if not isinstance(rec, dict):
            continue
        for k, v in rec.items():
            if not isinstance(v, str):
                continue
            s = v.strip()
            if "@" in s and _EMAIL_VAL_RE.search(s):
                email_cols.add(str(k))
            elif _IPV4_VAL_RE.match(s):
                ip_cols.add(str(k))
    if email_cols:
        findings.append(f"Email value(s) detected in column(s): {', '.join(sorted(email_cols))}")
    if ip_cols:
        findings.append(f"IPv4 value(s) detected in column(s): {', '.join(sorted(ip_cols))}")
    return findings


def _profile_json(path: Path, jsonl: bool) -> DataFileProfile:
    fmt = "jsonl" if jsonl else "json"
    try:
        raw_text = path.read_text(encoding="utf-8")
        if jsonl:
            records = [json.loads(line) for line in raw_text.splitlines() if line.strip()]
            cols = sorted(
                {k for r in records[:50] if isinstance(r, dict) for k in r},
            )
            return DataFileProfile(
                path=str(path),
                format=fmt,
                parseable=True,
                row_count=len(records),
                column_count=len(cols) if cols else None,
                columns=cols,
                pii_findings=_scan_json_records_for_pii(records, cols),
            )
        parsed: Any = json.loads(raw_text)
        if isinstance(parsed, list):
            cols = sorted({k for r in parsed[:50] if isinstance(r, dict) for k in r})
            return DataFileProfile(
                path=str(path),
                format=fmt,
                parseable=True,
                row_count=len(parsed),
                column_count=len(cols) if cols else None,
                columns=cols,
                pii_findings=_scan_json_records_for_pii(parsed, cols),
            )
        if isinstance(parsed, dict):
            cols = sorted(str(k) for k in parsed)
            return DataFileProfile(
                path=str(path),
                format=fmt,
                parseable=True,
                row_count=1,
                column_count=len(parsed),
                columns=cols,
                pii_findings=_scan_json_records_for_pii([parsed], cols),
            )
        return DataFileProfile(path=str(path), format=fmt, parseable=True, row_count=1)
    except Exception as exc:
        return DataFileProfile(
            path=str(path),
            format=fmt,
            parseable=False,
            parse_error=str(exc),
        )


def _profile_npy(path: Path) -> DataFileProfile:
    """Parse .npy header shape in pure Python without requiring numpy."""
    try:
        with path.open("rb") as fh:
            magic = fh.read(6)
            if magic != b"\x93NUMPY":
                return DataFileProfile(
                    path=str(path),
                    format="npy",
                    parseable=False,
                    parse_error="Invalid .npy magic header",
                )
            major, _minor = struct.unpack("BB", fh.read(2))
            if major == 1:
                header_len = struct.unpack("<H", fh.read(2))[0]
            else:
                header_len = struct.unpack("<I", fh.read(4))[0]
            header_text = fh.read(header_len).decode("latin1")
        shape_marker = "'shape':"
        idx = header_text.find(shape_marker)
        if idx == -1:
            return DataFileProfile(path=str(path), format="npy", parseable=True)
        paren_start = header_text.find("(", idx)
        paren_end = header_text.find(")", paren_start)
        dims = [
            int(part.strip())
            for part in header_text[paren_start + 1 : paren_end].split(",")
            if part.strip().isdigit()
        ]
        row_count = dims[0] if dims else None
        col_count = dims[1] if len(dims) > 1 else None
        return DataFileProfile(
            path=str(path),
            format="npy",
            parseable=True,
            row_count=row_count,
            column_count=col_count,
        )
    except Exception as exc:
        return DataFileProfile(
            path=str(path),
            format="npy",
            parseable=False,
            parse_error=str(exc),
        )


def _profile_parquet_or_magic(path: Path) -> DataFileProfile:
    """Validate Parquet magic bytes or use optional pyarrow if available."""
    try:
        import pyarrow.parquet as pq  # type: ignore[import-not-found]

        meta = pq.read_metadata(path)
        schema = pq.read_schema(path)
        return DataFileProfile(
            path=str(path),
            format="parquet",
            parseable=True,
            row_count=int(meta.num_rows),
            column_count=int(meta.num_columns),
            columns=list(schema.names),
        )
    except ImportError:
        try:
            with path.open("rb") as fh:
                head = fh.read(4)
                fh.seek(-4, 2)
                tail = fh.read(4)
            ok = head == b"PAR1" and tail == b"PAR1"
            return DataFileProfile(
                path=str(path),
                format="parquet",
                parseable=ok,
                parse_error=None if ok else "Invalid Parquet PAR1 magic bytes",
            )
        except Exception as exc:
            return DataFileProfile(
                path=str(path),
                format="parquet",
                parseable=False,
                parse_error=str(exc),
            )
    except Exception as exc:
        return DataFileProfile(
            path=str(path),
            format="parquet",
            parseable=False,
            parse_error=str(exc),
        )


def profile_data_file(path: Path) -> DataFileProfile:
    """Profile a data file into schema, dimensions, and parse status."""
    name = path.name.lower()
    if name.endswith(".csv.gz"):
        return _profile_delimited(path, ",", compressed=True)
    if name.endswith(".tsv.gz"):
        return _profile_delimited(path, "\t", compressed=True)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _profile_delimited(path, ",", compressed=False)
    if suffix == ".tsv":
        return _profile_delimited(path, "\t", compressed=False)
    if suffix == ".json":
        return _profile_json(path, jsonl=False)
    if suffix == ".jsonl":
        return _profile_json(path, jsonl=True)
    if suffix == ".npy":
        return _profile_npy(path)
    if suffix == ".parquet":
        return _profile_parquet_or_magic(path)
    # Non-empty check for binary formats requiring specialized R/Stata/HDF5 readers
    try:
        size = path.stat().st_size
        return DataFileProfile(
            path=str(path),
            format=suffix.lstrip(".") or "binary",
            parseable=size > 0,
            parse_error=None if size > 0 else "Zero-byte data file",
        )
    except Exception as exc:
        return DataFileProfile(
            path=str(path),
            format=suffix.lstrip(".") or "unknown",
            parseable=False,
            parse_error=str(exc),
        )
