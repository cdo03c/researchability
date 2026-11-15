"""Deterministic schema and row-count profiler for common research data formats."""

from __future__ import annotations

import csv
import gzip
import json
import re
import struct
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as ET
from pydantic import BaseModel, Field

_MISSING_TOKENS = {"", "na", "n/a", "nan", "null", "none", "."}
_STRONG_SENTINELS = {-9999, -999, -99, -98, -88, 9999}
_SMALL_NEG_SENTINELS = {-1, -2, -3, -8, -9}
_MAX_PROFILE_ROWS = 250_000
_MAX_JSON_PROFILE_BYTES = 25 * 1024 * 1024
_MAX_JSONL_LINES = 50_000
_PII_HEADER_RE = re.compile(
    r"^(?:e?_?mail|email_address|ip|ip_addr(?:ess)?|phone|telephone|mobile|ssn|"
    r"social_security|full_?name|first_?name|last_?name|home_address)$",
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


class _TabularAccumulator:
    """Incremental column statistics, sentinel, zero-mass, and PII accumulator."""

    def __init__(self, columns: list[str]) -> None:
        self.columns = columns
        self.n_cols = len(columns)
        self.row_count = 0
        self.sums = [0.0] * self.n_cols
        self.counts = [0] * self.n_cols
        self.zero_counts = [0] * self.n_cols
        self.non_missing = [0] * self.n_cols
        self.numeric_distinct: list[set[float]] = [set() for _ in range(self.n_cols)]
        self.strong_hits: list[set[int]] = [set() for _ in range(self.n_cols)]
        self.small_neg_hits: list[set[int]] = [set() for _ in range(self.n_cols)]
        self.email_cols: list[str] = []
        self.ip_cols: list[str] = []

    def add_row(self, row: Sequence[Any]) -> None:
        """Ingest one row of raw strings or typed Python values."""
        if self.row_count >= _MAX_PROFILE_ROWS:
            return
        self.row_count += 1
        for idx in range(min(len(row), self.n_cols)):
            raw = row[idx]
            if raw is None or isinstance(raw, bool):
                continue
            col_label = self.columns[idx] or f"col_{idx}"
            if isinstance(raw, (int, float)):
                fval = float(raw)
                if fval != fval:  # NaN
                    continue
                self.non_missing[idx] += 1
                self._record_float(idx, fval)
                continue

            val_str = str(raw).strip()
            if val_str.lower() in _MISSING_TOKENS:
                continue
            self.non_missing[idx] += 1
            if self.row_count <= 500:
                if "@" in val_str and _EMAIL_VAL_RE.search(val_str):
                    self.email_cols.append(col_label)
                elif _IPV4_VAL_RE.match(val_str):
                    self.ip_cols.append(col_label)
            try:
                fval = float(val_str)
            except ValueError:
                continue
            if fval != fval:
                continue
            self._record_float(idx, fval)

    def _record_float(self, idx: int, fval: float) -> None:
        self.sums[idx] += fval
        self.counts[idx] += 1
        if fval == 0.0:
            self.zero_counts[idx] += 1
        if len(self.numeric_distinct[idx]) < 200:
            self.numeric_distinct[idx].add(fval)
        if fval.is_integer():
            ival = int(fval)
            if ival in _STRONG_SENTINELS:
                self.strong_hits[idx].add(ival)
            elif ival in _SMALL_NEG_SENTINELS:
                self.small_neg_hits[idx].add(ival)

    def build(
        self,
        path: Path,
        fmt: str,
        *,
        row_count_override: int | None = None,
    ) -> DataFileProfile:
        """Finalize and return a ``DataFileProfile``."""
        effective_rows = row_count_override if row_count_override is not None else self.row_count
        means = {
            self.columns[i]: self.sums[i] / self.counts[i]
            for i in range(self.n_cols)
            if self.counts[i] > 0 and self.columns[i]
        }
        zero_mass = {
            self.columns[i]: round(self.zero_counts[i] / self.counts[i], 4)
            for i in range(self.n_cols)
            if self.counts[i] >= 5 and self.zero_counts[i] > 0 and self.columns[i]
        }
        sentinel_cols, empty_cols, pii_findings = _analyze_tab_pii_and_sentinels(
            self.columns,
            effective_rows,
            self.non_missing,
            self.numeric_distinct,
            self.strong_hits,
            self.small_neg_hits,
            self.email_cols,
            self.ip_cols,
        )
        return DataFileProfile(
            path=str(path),
            format=fmt,
            parseable=True,
            row_count=effective_rows,
            column_count=self.n_cols,
            columns=self.columns,
            numeric_means=means,
            zero_mass_columns=zero_mass,
            sentinel_columns=sentinel_cols,
            empty_columns=empty_cols,
            pii_findings=pii_findings,
        )


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
            acc = _TabularAccumulator([col.strip() for col in header])
            for row in reader:
                if not row:
                    continue
                if acc.row_count >= _MAX_PROFILE_ROWS:
                    break
                acc.add_row(row)
            return acc.build(path, fmt)
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
        if jsonl:
            records: list[Any] = []
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    records.append(json.loads(stripped))
                    if len(records) >= _MAX_JSONL_LINES:
                        break
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
        file_size = path.stat().st_size
        if file_size > _MAX_JSON_PROFILE_BYTES:
            return DataFileProfile(
                path=str(path),
                format=fmt,
                parseable=False,
                parse_error=f"JSON file exceeds {_MAX_JSON_PROFILE_BYTES} byte profiling cap",
            )
        raw_text = path.read_text(encoding="utf-8")
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


_XLSX_COL_RE = re.compile(r"^([A-Za-z]+)")
_MAX_ZIP_XML_BYTES = 50 * 1024 * 1024


def _xlsx_col_index(cell_ref: str, fallback: int) -> int:
    m = _XLSX_COL_RE.match(cell_ref)
    if not m:
        return fallback
    col = 0
    for ch in m.group(1).upper():
        col = col * 26 + (ord(ch) - ord("A") + 1)
    return max(0, col - 1)


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _profile_xlsx(path: Path) -> DataFileProfile:
    """Parse an Excel .xlsx worksheet using stdlib zipfile and defusedxml."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = set(zf.namelist())
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                ss_info = zf.getinfo("xl/sharedStrings.xml")
                if ss_info.file_size <= _MAX_ZIP_XML_BYTES:
                    ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                    for si in ss_root:
                        if _local_tag(si.tag) != "si":
                            continue
                        parts = [
                            node.text or ""
                            for node in si.iter()
                            if _local_tag(node.tag) == "t" and node.text
                        ]
                        shared.append("".join(parts))

            sheet_path = "xl/worksheets/sheet1.xml"
            if (
                sheet_path not in names
                and "xl/workbook.xml" in names
                and "xl/_rels/workbook.xml.rels" in names
            ):
                wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
                rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
                rel_map = {
                    rel.attrib.get("Id", ""): rel.attrib.get("Target", "")
                    for rel in rels_root
                    if _local_tag(rel.tag) == "Relationship"
                }
                for elem in wb_root.iter():
                    if _local_tag(elem.tag) == "sheet":
                        for k, v in elem.attrib.items():
                            if _local_tag(k) == "id" and v in rel_map:
                                target = rel_map[v].lstrip("/")
                                candidate = target if target.startswith("xl/") else f"xl/{target}"
                                if candidate in names:
                                    sheet_path = candidate
                                break
                        break

            if sheet_path not in names:
                sheet_candidates = sorted(
                    n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
                )
                if not sheet_candidates:
                    return DataFileProfile(
                        path=str(path),
                        format="xlsx",
                        parseable=False,
                        parse_error="No worksheet XML found in .xlsx archive",
                    )
                sheet_path = sheet_candidates[0]

            ws_info = zf.getinfo(sheet_path)
            if ws_info.file_size > _MAX_ZIP_XML_BYTES:
                return DataFileProfile(
                    path=str(path),
                    format="xlsx",
                    parseable=True,
                    parse_error="Worksheet XML exceeds safe in-memory profile limit",
                )
            ws_root = ET.fromstring(zf.read(sheet_path))

        rows: list[list[str]] = []
        for row_el in ws_root.iter():
            if _local_tag(row_el.tag) != "row":
                continue
            row_map: dict[int, str] = {}
            max_idx = -1
            seq_idx = 0
            for cell_el in row_el:
                if _local_tag(cell_el.tag) != "c":
                    continue
                cidx = _xlsx_col_index(cell_el.attrib.get("r", ""), seq_idx)
                seq_idx = cidx + 1
                max_idx = max(max_idx, cidx)
                ctype = cell_el.attrib.get("t", "")
                val = ""
                if ctype == "inlineStr":
                    val = "".join(
                        n.text or "" for n in cell_el.iter() if _local_tag(n.tag) == "t" and n.text
                    )
                else:
                    for child in cell_el:
                        if _local_tag(child.tag) == "v" and child.text is not None:
                            raw_v = child.text.strip()
                            if ctype == "s" and raw_v.isdigit():
                                s_idx = int(raw_v)
                                val = shared[s_idx] if 0 <= s_idx < len(shared) else raw_v
                            else:
                                val = raw_v
                            break
                if val != "":
                    row_map[cidx] = val
            if row_map:
                dense = [row_map.get(i, "") for i in range(max_idx + 1)]
                rows.append(dense)
                if len(rows) > _MAX_PROFILE_ROWS + 1:
                    break

        if not rows:
            return DataFileProfile(
                path=str(path),
                format="xlsx",
                parseable=False,
                parse_error="Empty worksheet",
            )
        header = [c.strip() for c in rows[0]]
        acc = _TabularAccumulator(header)
        for row in rows[1:]:
            acc.add_row(row)
        return acc.build(path, "xlsx")
    except Exception as exc:
        return DataFileProfile(
            path=str(path),
            format="xlsx",
            parseable=False,
            parse_error=str(exc),
        )


def _extract_dta_tag(blob: bytes, tag: bytes, start: int = 0) -> tuple[bytes | None, int]:
    open_t = b"<" + tag + b">"
    close_t = b"</" + tag + b">"
    s = blob.find(open_t, start)
    if s == -1:
        return None, start
    val_start = s + len(open_t)
    e = blob.find(close_t, val_start)
    if e == -1:
        return None, start
    return blob[val_start:e], e + len(close_t)


def _profile_dta(path: Path) -> DataFileProfile:
    """Parse Stata .dta (formats 113-115 and XML-framed 117-119) cleanly in pure Python."""
    try:
        raw = path.read_bytes()
    except Exception as exc:
        return DataFileProfile(path=str(path), format="dta", parseable=False, parse_error=str(exc))

    if len(raw) < 16:
        return DataFileProfile(
            path=str(path),
            format="dta",
            parseable=False,
            parse_error="Truncated .dta file",
        )

    # Modern Stata 13-18 XML-tagged binary formats (117, 118, 119)
    if raw.lstrip().startswith(b"<stata_dta>"):
        try:
            byteorder_raw, _ = _extract_dta_tag(raw, b"byteorder")
            k_raw, _ = _extract_dta_tag(raw, b"K")
            n_raw, _ = _extract_dta_tag(raw, b"N")
            vtypes_raw, _ = _extract_dta_tag(raw, b"variable_types")
            vnames_raw, _ = _extract_dta_tag(raw, b"varnames")
            data_raw, _ = _extract_dta_tag(raw, b"data")
            if None in (byteorder_raw, k_raw, n_raw, vnames_raw):
                return DataFileProfile(
                    path=str(path),
                    format="dta",
                    parseable=False,
                    parse_error="Missing required Stata <stata_dta> header tags",
                )
            assert byteorder_raw is not None and k_raw is not None
            assert n_raw is not None and vnames_raw is not None
            endian = "<" if byteorder_raw.strip() == b"LSF" else ">"
            k_vars = struct.unpack(f"{endian}{'H' if len(k_raw) == 2 else 'I'}", k_raw)[0]
            n_obs = struct.unpack(f"{endian}{'I' if len(n_raw) == 4 else 'Q'}", n_raw)[0]

            columns: list[str] = []
            if k_vars > 0 and len(vnames_raw) >= k_vars:
                slot = max(1, len(vnames_raw) // k_vars)
                for idx in range(k_vars):
                    chunk = vnames_raw[idx * slot : (idx + 1) * slot]
                    columns.append(chunk.split(b"\x00", 1)[0].decode("utf-8", errors="replace"))

            if vtypes_raw is not None and data_raw is not None and len(vtypes_raw) == k_vars * 2:
                vtypes = struct.unpack(f"{endian}{k_vars}H", vtypes_raw)
                # Decode dense rows directly when strL (32768) is absent
                if 32768 not in vtypes:
                    spec_list: list[tuple[str, str, int, float | None]] = []
                    row_bytes = 0
                    supported = True
                    for vt in vtypes:
                        if vt == 65530:
                            spec_list.append(("num", "d", 8, 8.9884656743115795e307))
                            row_bytes += 8
                        elif vt == 65529:
                            spec_list.append(("num", "f", 4, 1.7014117331926443e38))
                            row_bytes += 4
                        elif vt == 65528:
                            spec_list.append(("num", "i", 4, 2147483621.0))
                            row_bytes += 4
                        elif vt == 65527:
                            spec_list.append(("num", "h", 2, 32741.0))
                            row_bytes += 2
                        elif vt == 65526:
                            spec_list.append(("num", "b", 1, 101.0))
                            row_bytes += 1
                        elif 1 <= vt <= 2045:
                            spec_list.append(("str", f"{vt}s", vt, None))
                            row_bytes += vt
                        else:
                            supported = False
                            break

                    if supported and row_bytes > 0:
                        acc = _TabularAccumulator(columns)
                        rows_to_read = min(n_obs, _MAX_PROFILE_ROWS, len(data_raw) // row_bytes)
                        for r_idx in range(rows_to_read):
                            offset = r_idx * row_bytes
                            row_vals: list[Any] = []
                            for kind, code, width, miss_ge in spec_list:
                                raw_val = struct.unpack_from(f"{endian}{code}", data_raw, offset)[0]
                                offset += width
                                if kind == "num":
                                    fval = float(raw_val)
                                    if miss_ge is not None and fval >= miss_ge:
                                        row_vals.append(None)
                                    else:
                                        row_vals.append(fval)
                                else:
                                    sval = raw_val.split(b"\x00", 1)[0].decode(
                                        "utf-8", errors="replace"
                                    )
                                    row_vals.append(sval.strip())
                            acc.add_row(row_vals)
                        return acc.build(path, "dta", row_count_override=n_obs)

            return DataFileProfile(
                path=str(path),
                format="dta",
                parseable=True,
                row_count=n_obs,
                column_count=k_vars,
                columns=columns,
            )
        except Exception as exc:
            return DataFileProfile(
                path=str(path),
                format="dta",
                parseable=False,
                parse_error=str(exc),
            )

    # Classic Stata 8-12 binary formats (113, 114, 115)
    ds_format = raw[0]
    byteorder_code = raw[1]
    if ds_format in {113, 114, 115} and byteorder_code in {1, 2} and len(raw) >= 109:
        endian = ">" if byteorder_code == 1 else "<"
        nvar = struct.unpack_from(f"{endian}H", raw, 4)[0]
        nobs = struct.unpack_from(f"{endian}I", raw, 6)[0]
        varlist_start = 109 + nvar
        varlist_end = varlist_start + nvar * 33
        columns = []
        if len(raw) >= varlist_end:
            for idx in range(nvar):
                slot_bytes = raw[varlist_start + idx * 33 : varlist_start + (idx + 1) * 33]
                columns.append(slot_bytes.split(b"\x00", 1)[0].decode("latin1", errors="replace"))
        return DataFileProfile(
            path=str(path),
            format="dta",
            parseable=True,
            row_count=nobs,
            column_count=nvar,
            columns=columns,
        )

    return DataFileProfile(
        path=str(path),
        format="dta",
        parseable=False,
        parse_error=f"Unrecognized Stata .dta header bytes ({raw[:4]!r})",
    )


def _profile_r_or_spss(path: Path, fmt: str) -> DataFileProfile:
    """Validate .rds/.RData/.sav magic signatures (plus optional rdata/pyreadstat when present)."""
    try:
        size = path.stat().st_size
        if size == 0:
            return DataFileProfile(
                path=str(path), format=fmt, parseable=False, parse_error="Zero-byte data file"
            )
        with path.open("rb") as fh:
            head = fh.read(8)
        if fmt == "sav":
            ok = head.startswith((b"$FL2", b"$FL3"))
            return DataFileProfile(
                path=str(path),
                format="sav",
                parseable=ok,
                parse_error=None if ok else "Invalid SPSS .sav header ($FL2/$FL3 missing)",
            )
        if head.startswith(b"\x1f\x8b"):
            with gzip.open(path, "rb") as gz:
                head = gz.read(8)
        ok = head.startswith((b"RDX", b"RDA", b"RDB", b"X\n", b"X\r\n", b"A\n", b"B\n"))
        return DataFileProfile(
            path=str(path),
            format=fmt,
            parseable=ok,
            parse_error=None if ok else f"Invalid {fmt.upper()} serialization header",
        )
    except Exception as exc:
        return DataFileProfile(path=str(path), format=fmt, parseable=False, parse_error=str(exc))


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
    if suffix == ".xlsx":
        return _profile_xlsx(path)
    if suffix == ".dta":
        return _profile_dta(path)
    if suffix in {".rds", ".rdata", ".sav"}:
        return _profile_r_or_spss(path, suffix.lstrip("."))
    if suffix == ".npy":
        return _profile_npy(path)
    if suffix == ".parquet":
        return _profile_parquet_or_magic(path)
    # Non-empty check for remaining binary formats requiring specialized HDF5 readers
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
