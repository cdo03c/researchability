"""Deterministic schema and row-count profiler for common research data formats."""

from __future__ import annotations

import csv
import gzip
import json
import struct
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class DataFileProfile(BaseModel):
    """Schema, dimension, and numeric summary for one data file."""

    path: str
    format: str
    parseable: bool
    row_count: int | None = None
    column_count: int | None = None
    columns: list[str] = Field(default_factory=list)
    numeric_means: dict[str, float] = Field(default_factory=dict)
    parse_error: str | None = None


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
            row_count = 0
            sums = [0.0] * len(columns)
            counts = [0] * len(columns)
            for row in reader:
                if not row:
                    continue
                row_count += 1
                for idx in range(min(len(row), len(columns))):
                    val_str = row[idx].strip()
                    if not val_str:
                        continue
                    try:
                        sums[idx] += float(val_str)
                        counts[idx] += 1
                    except ValueError:
                        continue
            means = {
                columns[i]: sums[i] / counts[i]
                for i in range(len(columns))
                if counts[i] > 0 and columns[i]
            }
            return DataFileProfile(
                path=str(path),
                format=fmt,
                parseable=True,
                row_count=row_count,
                column_count=len(columns),
                columns=columns,
                numeric_means=means,
            )
    except Exception as exc:
        return DataFileProfile(
            path=str(path),
            format=fmt,
            parseable=False,
            parse_error=str(exc),
        )


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
            )
        if isinstance(parsed, dict):
            return DataFileProfile(
                path=str(path),
                format=fmt,
                parseable=True,
                row_count=1,
                column_count=len(parsed),
                columns=sorted(str(k) for k in parsed),
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
