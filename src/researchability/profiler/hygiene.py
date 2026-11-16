"""Static repository hygiene, README inventory, path, collision, and dependency audits."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

_PLAIN_TEXT_README_EXTS = {".md", ".markdown", ".txt", ".rst", ""}
_BINARY_README_EXTS = {".doc", ".docx", ".pdf", ".rtf"}
_CODE_EXTENSIONS = {".py", ".r", ".rmd", ".do", ".ado", ".sas", ".sps", ".jl", ".m", ".ipynb"}
_DATA_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".parquet",
    ".json",
    ".jsonl",
    ".rds",
    ".rdata",
    ".dta",
    ".xlsx",
    ".xls",
    ".npy",
    ".npz",
    ".sav",
}

_README_FILE_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_/:.-])([A-Za-z0-9_./-]+\.(?:"
    r"py|r|rmd|do|ado|sas|sps|jl|m|ipynb|csv|tsv|parquet|json|jsonl|"
    r"rds|rdata|dta|xlsx|xls|npy|npz|sav|shp|geojson|zip|sh|bash"
    r"))\b",
    re.IGNORECASE,
)

# Static script file read/source and write patterns across R, Python, Stata
_SCRIPT_READ_PATTERNS = [
    # Python pandas/numpy/json/open
    re.compile(
        r"""(?:read_csv|read_parquet|read_excel|read_stata|read_feather|read_json|np\.load|loadtxt)\s*\(\s*["']([^"']+)["']""",
        re.IGNORECASE,
    ),
    # R read/load/source
    re.compile(
        r"""(?:read\.csv|read_csv|read\.delim|read_tsv|readRDS|read_dta|haven::read_dta|load|source|st_read)\s*\(\s*["']([^"']+)["']""",
        re.IGNORECASE,
    ),
    # Stata use / import delimited / do / merge
    re.compile(
        r"""^\s*(?:use|import\s+delimited(?:\s+using)?|do|run|merge\s+[^\n]*?\s+using)\s+["']?([^\s"',]+\.(?:dta|csv|tsv|txt|do))["']?""",
        re.IGNORECASE | re.MULTILINE,
    ),
]

_SCRIPT_WRITE_PATTERNS = [
    # Python pandas/numpy/matplotlib
    re.compile(
        r"""(?:to_csv|to_parquet|to_json|to_excel|savefig|np\.save)\s*\(\s*["']([^"']+)["']""",
        re.IGNORECASE,
    ),
    # R write/save/ggsave/devices/stargazer
    re.compile(
        r"""(?:write\.csv|write_csv|write_tsv|saveRDS|ggsave|pdf|png|svg|tiff)\s*\(\s*(?:(?:file|filename)\s*=\s*)?["']([^"']+)["']""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""stargazer\s*\([^\n)]*out\s*=\s*["']([^"']+)["']""",
        re.IGNORECASE,
    ),
    # Stata save / esttab / outreg2 / graph export (captures trailing options)
    re.compile(
        r"""^\s*(?:save|esttab\b[^\n]*?\s+using|outreg2\b[^\n]*?\s+using|graph\s+export)\s+["']?([^\s"',]+)["']?(.*)""",
        re.IGNORECASE | re.MULTILINE,
    ),
]

_HARDCODED_DIR_RE = re.compile(
    r"""(?:setwd\s*\(\s*["']([^"']+)["']|\bcd\s+["']?([A-Za-z]:\\[^\s"']+|/(?:Users|home|Volumes|mnt|Dropbox)/[^\s"']+|~/[^\s"']+)["']?)""",
    re.IGNORECASE,
)

# Unguarded negative/sentinel comparisons and NA-to-0 patterns in R/Python/Stata
_UNGUARDED_THRESHOLD_RE = re.compile(
    r"""\b([A-Za-z][A-Za-z0-9_]{1,30})\s*(?:<=?)\s*(\d+(?:\.\d+)?)\b"""
)
_BLIND_ZERO_IMPUTE_RE = re.compile(
    r"""(?:\.fillna\s*\(\s*0\s*\)|replace_na\s*\(\s*0\s*\)|"""
    r"""ifelse\s*\(\s*is\.na\s*\([^)]+\)\s*,\s*0\s*,|"""
    r"""recode\s+[^\n]*?\.\s*=\s*0\b)""",
    re.IGNORECASE,
)
_SENTINEL_GUARD_RE = re.compile(
    r"""(?:>=\s*0|>=\s*-?\d|is\.na|!is\.na|notna|dropna|mvdecode|inrange)""",
    re.IGNORECASE,
)

# Language import parsing
_PY_IMPORT_RE = re.compile(r"^\s*import\s+([A-Za-z0-9_.,\s]+?)(?:\s+as\s+\w+)?$", re.MULTILINE)
_PY_FROM_RE = re.compile(
    r"^\s*from\s+([A-Za-z0-9_]+)(?:\.[A-Za-z0-9_.]+)?\s+import\b", re.MULTILINE
)
_R_LIB_RE = re.compile(
    r"""\b(?:library|require|p_load)\s*\(\s*["']?([A-Za-z0-9_.]+)["']?\s*\)""",
    re.IGNORECASE,
)
_R_GITHUB_RE = re.compile(
    r"""(?:devtools|remotes|pak)::(?:install_github|pkg_install)\s*\(\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_STATA_SSC_RE = re.compile(r"^\s*ssc\s+install\s+([A-Za-z0-9_]+)", re.IGNORECASE | re.MULTILINE)

_PYTHON_STDLIB = frozenset(m.lower() for m in sys.stdlib_module_names)
_R_BASE_PACKAGES = {
    "base",
    "compiler",
    "datasets",
    "grdevices",
    "graphics",
    "grid",
    "methods",
    "parallel",
    "splines",
    "stats",
    "stats4",
    "tcltk",
    "tools",
    "utils",
}


class ReadmeInventoryAudit(BaseModel):
    """Audit comparing README documented files and entrypoints against the repo tree."""

    readme_files: list[str] = Field(default_factory=list)
    non_plain_text_readmes: list[str] = Field(default_factory=list)
    referenced_files: list[str] = Field(default_factory=list)
    stale_references: list[str] = Field(default_factory=list)
    undocumented_code_files: list[str] = Field(default_factory=list)
    undocumented_data_files: list[str] = Field(default_factory=list)
    competing_entrypoints: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


class DuplicateFileGroup(BaseModel):
    """Files sharing a basename across subdirectories."""

    basename: str
    paths: list[str]
    divergent_content: bool = False


class OutputCollision(BaseModel):
    """Repeated non-append write target detected across or within scripts."""

    target: str
    writers: list[str]


class DependencyAuditResult(BaseModel):
    """Summary of script imports vs manifest declarations and optional registry liveness."""

    imported_python: list[str] = Field(default_factory=list)
    imported_r: list[str] = Field(default_factory=list)
    imported_stata: list[str] = Field(default_factory=list)
    undeclared_packages: list[str] = Field(default_factory=list)
    unpinned_remote_packages: list[str] = Field(default_factory=list)
    archived_or_missing_packages: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


class ScriptPathAuditResult(BaseModel):
    """Results of static cross-reference audit across code files."""

    missing_input_references: list[str] = Field(default_factory=list)
    hardcoded_dirs: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


def audit_readme_inventory(
    readme_texts: dict[str, str],
    code_files: list[str],
    data_files: list[str],
) -> ReadmeInventoryAudit:
    """Compare files mentioned in README against actual code/data artifacts."""
    non_plain = [p for p in readme_texts if Path(p).suffix.lower() in _BINARY_README_EXTS]
    combined_readme = "\n".join(
        body
        for path, body in readme_texts.items()
        if Path(path).suffix.lower() in _PLAIN_TEXT_README_EXTS
    )

    known_paths = {p.replace("\\", "/").lstrip("./") for p in [*code_files, *data_files]}
    known_basenames = {Path(p).name.lower(): p for p in known_paths}

    refs: list[str] = []
    stale: list[str] = []
    seen_refs: set[str] = set()
    for match in _README_FILE_REF_RE.finditer(combined_readme):
        raw = match.group(1).replace("\\", "/").lstrip("./")
        base = Path(raw).name.lower()
        if raw in seen_refs or base.startswith("readme."):
            continue
        seen_refs.add(raw)
        refs.append(raw)
        if (
            raw not in known_paths
            and base not in known_basenames
            and not any(tok in base for tok in ("example", "sample_out", "output"))
        ):
            stale.append(raw)

    mentioned_basenames = {Path(r).name.lower() for r in refs}
    undoc_code = [
        p
        for p in code_files
        if Path(p).suffix.lower() in _CODE_EXTENSIONS
        and Path(p).name.lower() not in mentioned_basenames
        and p.replace("\\", "/") not in combined_readme
    ]
    undoc_data = [
        p
        for p in data_files
        if Path(p).suffix.lower() in _DATA_EXTENSIONS
        and Path(p).name.lower() not in mentioned_basenames
        and p.replace("\\", "/") not in combined_readme
    ]

    # Detect competing top-level/versioned entrypoints (e.g., Analysis_FINAL.R vs Analysis_update.R)
    versioned_candidates = [
        p
        for p in code_files
        if re.search(r"(?:final|update|v\d+|new|old|backup|copy|revised)", Path(p).stem, re.I)
    ]
    competing = versioned_candidates if len(versioned_candidates) >= 2 else []

    flags: list[str] = []
    if non_plain:
        flags.append(f"NON_PLAIN_TEXT_README: {', '.join(non_plain)}")
    if stale:
        flags.append(f"STALE_README_FILE_REF: {', '.join(stale[:5])}")
    if len(code_files) >= 3 and undoc_code:
        flags.append(f"UNDOCUMENTED_CODE_FILES: {', '.join(undoc_code[:5])}")
    if len(data_files) >= 2 and undoc_data and mentioned_basenames:
        flags.append(f"UNDOCUMENTED_DATA_FILES: {', '.join(undoc_data[:5])}")
    if competing:
        flags.append(f"COMPETING_ENTRYPOINTS: {', '.join(competing[:5])}")

    return ReadmeInventoryAudit(
        readme_files=list(readme_texts.keys()),
        non_plain_text_readmes=non_plain,
        referenced_files=refs,
        stale_references=stale,
        undocumented_code_files=undoc_code,
        undocumented_data_files=undoc_data,
        competing_entrypoints=competing,
        flags=flags,
    )


def _sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def detect_duplicate_files(workdir: Path | None, rel_paths: list[str]) -> list[DuplicateFileGroup]:
    """Find identically named files across subdirectories and flag content divergence."""
    by_base: dict[str, list[str]] = defaultdict(list)
    for rel in rel_paths:
        clean = rel.replace("\\", "/")
        by_base[Path(clean).name.lower()].append(clean)

    groups: list[DuplicateFileGroup] = []
    for base, paths in sorted(by_base.items()):
        unique_paths = sorted(set(paths))
        if len(unique_paths) < 2:
            continue
        divergent = False
        if workdir is not None:
            hashes = {_sha256_file(workdir / p) for p in unique_paths if (workdir / p).is_file()}
            hashes.discard(None)
            divergent = len(hashes) > 1
        groups.append(
            DuplicateFileGroup(
                basename=base,
                paths=unique_paths,
                divergent_content=divergent,
            )
        )
    return groups


def detect_output_collisions(code_texts: dict[str, str]) -> list[OutputCollision]:
    """Detect output file paths written multiple times without append mode."""
    writers_by_target: dict[str, list[str]] = defaultdict(list)

    for rel_path, body in code_texts.items():
        for pat in _SCRIPT_WRITE_PATTERNS:
            for match in pat.finditer(body):
                target = match.group(1).replace("\\", "/").strip()
                if not target or "{" in target or "%" in target:
                    continue
                tail = match.group(2) if match.lastindex and match.lastindex >= 2 else ""
                if "append" in tail.lower():
                    continue
                norm_target = target.lstrip("./")
                writers_by_target[norm_target].append(rel_path)

    collisions: list[OutputCollision] = []
    for target, writers in sorted(writers_by_target.items()):
        if len(writers) >= 2:
            collisions.append(OutputCollision(target=target, writers=writers))
    return collisions


def audit_script_paths(
    workdir: Path | None,
    code_texts: dict[str, str],
    all_known_files: list[str],
) -> ScriptPathAuditResult:
    """Verify static input-file references resolve to workspace files or upstream script outputs."""
    known_rel = {p.replace("\\", "/").lstrip("./") for p in all_known_files}
    known_names = {Path(p).name.lower() for p in known_rel}

    produced_rel: set[str] = set()
    produced_names: set[str] = set()
    for body in code_texts.values():
        for pat in _SCRIPT_WRITE_PATTERNS:
            for m in pat.finditer(body):
                t = m.group(1).replace("\\", "/").lstrip("./")
                produced_rel.add(t)
                produced_names.add(Path(t).name.lower())

    missing_refs: list[str] = []
    hardcoded_dirs: list[str] = []

    for rel_script, body in code_texts.items():
        for m in _HARDCODED_DIR_RE.finditer(body):
            dir_val = m.group(1) or m.group(2)
            if dir_val:
                hardcoded_dirs.append(f"{rel_script}: {dir_val}")

        script_dir = Path(rel_script).parent
        for pat in _SCRIPT_READ_PATTERNS:
            for m in pat.finditer(body):
                raw_target = m.group(1).replace("\\", "/").strip()
                if not raw_target or "://" in raw_target or "{" in raw_target:
                    continue
                norm = raw_target.lstrip("./")
                name = Path(norm).name.lower()
                if (
                    norm in known_rel
                    or name in known_names
                    or norm in produced_rel
                    or name in produced_names
                ):
                    continue
                if workdir is not None and (
                    (workdir / norm).exists() or (workdir / script_dir / norm).exists()
                ):
                    continue
                missing_refs.append(f"{rel_script} -> {raw_target}")

    flags: list[str] = []
    if hardcoded_dirs:
        flags.append(f"HARDCODED_LOCAL_PATH: {hardcoded_dirs[0]}")
    if missing_refs:
        flags.append(f"MISSING_REFERENCED_FILE: {', '.join(missing_refs[:5])}")

    return ScriptPathAuditResult(
        missing_input_references=sorted(set(missing_refs)),
        hardcoded_dirs=sorted(set(hardcoded_dirs)),
        flags=flags,
    )


def _record_pkg_name(declared: set[str], raw_name: str) -> None:
    cleaned = raw_name.strip().split("::")[-1].strip("'\"")
    match = re.match(r"^([A-Za-z0-9_.-]+)", cleaned)
    if not match:
        return
    name = match.group(1).lower()
    if name in {"python", "pip", "conda"}:
        return
    declared.add(name)
    declared.add(name.replace("-", "_"))


def _extract_declared_packages(manifest_texts: dict[str, str]) -> set[str]:
    declared: set[str] = set()
    for path, body in manifest_texts.items():
        name = Path(path).name.lower()
        if name == "dockerfile":
            continue
        if name == "renv.lock":
            try:
                data = json.loads(body)
                pkgs = data.get("Packages", {})
                if isinstance(pkgs, dict):
                    for pkg_name in pkgs:
                        _record_pkg_name(declared, str(pkg_name))
                    continue
            except json.JSONDecodeError:
                pass
        elif name == "pyproject.toml":
            try:
                toml_data = tomllib.loads(body)
                project = toml_data.get("project", {})
                if isinstance(project, dict):
                    for dep in project.get("dependencies", []) or []:
                        if isinstance(dep, str):
                            _record_pkg_name(declared, dep)
                    opt = project.get("optional-dependencies", {}) or {}
                    if isinstance(opt, dict):
                        for group_deps in opt.values():
                            if isinstance(group_deps, list):
                                for dep in group_deps:
                                    if isinstance(dep, str):
                                        _record_pkg_name(declared, dep)
                poetry = toml_data.get("tool", {}).get("poetry", {})
                if isinstance(poetry, dict):
                    for pkg_name in poetry.get("dependencies", {}) or {}:
                        _record_pkg_name(declared, str(pkg_name))
                    for group in (poetry.get("group", {}) or {}).values():
                        if isinstance(group, dict):
                            for pkg_name in group.get("dependencies", {}) or {}:
                                _record_pkg_name(declared, str(pkg_name))
                continue
            except tomllib.TOMLDecodeError:
                pass
        elif name in {"environment.yml", "environment.yaml", "conda.yml"}:
            try:
                yml_data = yaml.safe_load(body)
                if isinstance(yml_data, dict):
                    for entry in yml_data.get("dependencies", []) or []:
                        if isinstance(entry, str):
                            _record_pkg_name(declared, entry)
                        elif isinstance(entry, dict):
                            for pip_dep in entry.get("pip", []) or []:
                                if isinstance(pip_dep, str):
                                    _record_pkg_name(declared, pip_dep)
                    continue
            except yaml.YAMLError:
                pass
        elif name in {"poetry.lock", "uv.lock"}:
            for m in re.finditer(r'^\s*name\s*=\s*"([^"]+)"', body, re.MULTILINE):
                _record_pkg_name(declared, m.group(1))
            continue

        for raw_line in body.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or line.startswith(("[", "dependencies:", "channels:", "-r", "--")):
                continue
            if line.startswith("- "):
                line = line[2:].strip()
            _record_pkg_name(declared, line)
    return declared


def _default_registry_http_get(url: str) -> tuple[int, str]:
    from researchability.connectors.base import create_safe_http_client

    with create_safe_http_client(timeout=8.0) as client:
        resp = client.get(url)
        return resp.status_code, resp.text


def audit_dependencies(
    code_texts: dict[str, str],
    manifest_texts: dict[str, str],
    *,
    check_registry: bool = False,
    http_get: Callable[[str], tuple[int, str]] | None = None,
) -> DependencyAuditResult:
    """Audit code imports against manifests and optionally check CRAN/PyPI liveness."""
    py_imports: set[str] = set()
    r_imports: set[str] = set()
    stata_pkgs: set[str] = set()
    unpinned_remote: set[str] = set()

    local_stems = {Path(p).stem.lower() for p in code_texts}

    for path, body in code_texts.items():
        ext = Path(path).suffix.lower()
        if ext in {".py", ".ipynb"}:
            for m in _PY_IMPORT_RE.finditer(body):
                for part in m.group(1).split(","):
                    mod = part.strip().split(".")[0]
                    if mod and mod.lower() not in _PYTHON_STDLIB and mod.lower() not in local_stems:
                        py_imports.add(mod)
            for m in _PY_FROM_RE.finditer(body):
                mod = m.group(1)
                if mod and mod.lower() not in _PYTHON_STDLIB and mod.lower() not in local_stems:
                    py_imports.add(mod)
        elif ext in {".r", ".rmd"}:
            for m in _R_LIB_RE.finditer(body):
                pkg = m.group(1)
                if pkg.lower() not in _R_BASE_PACKAGES:
                    r_imports.add(pkg)
            for m in _R_GITHUB_RE.finditer(body):
                spec = m.group(1)
                if "@" not in spec and "ref=" not in body[m.end() : m.end() + 40]:
                    unpinned_remote.add(spec)
        elif ext in {".do", ".ado"}:
            for m in _STATA_SSC_RE.finditer(body):
                stata_pkgs.add(m.group(1))

    declared = _extract_declared_packages(manifest_texts)
    undeclared: list[str] = []
    if manifest_texts:
        for pkg in sorted(py_imports):
            norm = pkg.lower().replace("-", "_")
            if pkg.lower() not in declared and norm not in declared:
                undeclared.append(f"python:{pkg}")
        for pkg in sorted(r_imports):
            if pkg.lower() not in declared:
                undeclared.append(f"r:{pkg}")

    archived_or_missing: list[str] = []
    if check_registry:
        fetcher = http_get or _default_registry_http_get
        for pkg in sorted(py_imports):
            try:
                status, _ = fetcher(f"https://pypi.org/pypi/{pkg}/json")
                if status == 404:
                    archived_or_missing.append(f"pypi:{pkg}")
            except Exception:
                continue
        for pkg in sorted(r_imports):
            try:
                status, body = fetcher(f"https://crandb.r-pkg.org/{pkg}")
                if status == 404:
                    archived_or_missing.append(f"cran:{pkg}")
                elif status == 200 and '"archived"' in body.lower():
                    archived_or_missing.append(f"cran:{pkg} (archived)")
            except Exception:
                continue

    flags: list[str] = []
    if undeclared:
        flags.append(f"UNDECLARED_IMPORTS: {', '.join(undeclared[:8])}")
    if unpinned_remote:
        flags.append(f"UNPINNED_NON_CRAN_PACKAGE: {', '.join(sorted(unpinned_remote)[:5])}")
    if archived_or_missing:
        flags.append(f"ARCHIVED_OR_MISSING_PACKAGE: {', '.join(archived_or_missing[:5])}")

    return DependencyAuditResult(
        imported_python=sorted(py_imports),
        imported_r=sorted(r_imports),
        imported_stata=sorted(stata_pkgs),
        undeclared_packages=undeclared,
        unpinned_remote_packages=sorted(unpinned_remote),
        archived_or_missing_packages=archived_or_missing,
        flags=flags,
    )


def scan_code_sentinel_risks(
    code_texts: dict[str, str],
    sentinel_columns: dict[str, list[str]] | None = None,
) -> list[str]:
    """Flag unguarded threshold slicing on sentinel-contaminated columns or blind NA->0 coercion."""
    flags: list[str] = []
    sentinel_cols_lower = {k.lower(): k for k in (sentinel_columns or {})}

    for path, body in code_texts.items():
        if _BLIND_ZERO_IMPUTE_RE.search(body):
            flags.append(f"BLIND_NA_TO_ZERO in {path}")
        if not sentinel_cols_lower:
            continue
        for line in body.splitlines():
            m = _UNGUARDED_THRESHOLD_RE.search(line)
            if not m:
                continue
            var = m.group(1)
            if var.lower() in sentinel_cols_lower and not _SENTINEL_GUARD_RE.search(line):
                flags.append(
                    f"UNGUARDED_SENTINEL_FILTER: {path} compares '{var}' ({m.group(0)}) "
                    "without filtering negative/sentinel codes"
                )
    return flags
