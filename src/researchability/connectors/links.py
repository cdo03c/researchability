"""Shared URL/DOI extraction, host classification, and workspace file scanning."""

from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

from researchability.models import ArtifactLink

_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)\}]+", re.IGNORECASE)
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b")

_CODE_EXTENSIONS = {
    ".py",
    ".r",
    ".rmd",
    ".ipynb",
    ".sh",
    ".bash",
    ".jl",
    ".m",
    ".do",
    ".ado",
    ".sas",
    ".sps",
    ".c",
    ".cpp",
    ".rs",
}
_DATA_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".tab",
    ".parquet",
    ".json",
    ".jsonl",
    ".rds",
    ".rdata",
    ".dta",
    ".sav",
    ".h5",
    ".h5ad",
    ".hdf5",
    ".npz",
    ".npy",
    ".xlsx",
    ".xls",
    ".fastq",
    ".bam",
    ".vcf",
    ".bed",
}
_MANIFEST_NAMES = {
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "environment.yml",
    "environment.yaml",
    "pipfile",
    "pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "renv.lock",
    "description",
    "dockerfile",
    "conda.yml",
    "setup.py",
    "setup.cfg",
}

_HOST_RULES: list[tuple[str, str, str]] = [
    ("github.com", "github", "code"),
    ("gitlab.com", "gitlab", "code"),
    ("bitbucket.org", "bitbucket", "code"),
    ("huggingface.co", "huggingface", "code"),
    ("zenodo.org", "zenodo", "data"),
    ("dataverse.harvard.edu", "dataverse", "data"),
    ("osf.io", "osf", "data"),
    ("figshare.com", "figshare", "data"),
    ("ncbi.nlm.nih.gov", "ncbi_geo", "data"),
    ("protocols.io", "protocols_io", "protocol"),
    ("bio-protocol.org", "protocols_io", "protocol"),
    ("aspredicted.org", "aspredicted", "prereg"),
    ("psyarxiv.com", "psyarxiv", "other"),
    ("openicpsr.org", "icpsr", "data"),
    ("icpsr.umich.edu", "icpsr", "data"),
    ("clinicaltrials.gov", "clinicaltrials", "prereg"),
    ("scicrunch.org", "rrid", "protocol"),
]


def _clean_match(raw: str) -> str:
    return raw.rstrip(".,;:!?)\"'\\")


def _hostname_matches(hostname: str, domain: str) -> bool:
    """Return True if hostname equals domain or is a strict subdomain of it."""
    host = hostname.rstrip(".").lower()
    dom = domain.lower()
    return host == dom or host.endswith(f".{dom}")


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_safe_public_url(url: str, *, resolve_dns: bool = False) -> bool:
    """Reject non-HTTP(S) schemes, localhost, internal suffixes, and private/link-local IPs.

    When ``resolve_dns`` is True, resolves the hostname via ``socket.getaddrinfo`` and
    rejects any hostname that resolves to a non-public IP address.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname:
        return False
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
        (".local", ".internal", ".localhost")
    ):
        return False
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        if not resolve_dns:
            return True
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        try:
            infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except OSError:
            return True
        for info in infos:
            sockaddr = info[4]
            if not sockaddr:
                continue
            raw_ip = str(sockaddr[0]).split("%", 1)[0]
            try:
                resolved_ip = ipaddress.ip_address(raw_ip)
            except ValueError:
                return False
            if not _is_public_ip(resolved_ip):
                return False
        return True
    return _is_public_ip(ip)


def safe_resolve_under(root: Path, rel_path: str | Path) -> Path | None:
    """Resolve rel_path under root, returning None on absolute paths or directory traversal."""
    candidate_rel = Path(rel_path)
    if candidate_rel.is_absolute() or ".." in candidate_rel.parts:
        return None
    root_resolved = root.resolve()
    target = (root_resolved / candidate_rel).resolve()
    if not target.is_relative_to(root_resolved):
        return None
    return target


def classify_url(url: str) -> tuple[str, str]:
    """Return (host_label, artifact_kind) for a candidate artifact URL."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    for domain_fragment, host_label, default_kind in _HOST_RULES:
        if _hostname_matches(hostname, domain_fragment):
            if host_label == "huggingface" and "/datasets/" in path:
                return host_label, "data"
            if host_label == "osf" and "registr" in path:
                return host_label, "prereg"
            return host_label, default_kind
    if _hostname_matches(hostname, "doi.org"):
        if "10.7910/dvn" in path:
            return "dataverse", "data"
        if "10.3886/" in path:
            return "icpsr", "data"
        if "10.17504/protocols.io" in path:
            return "protocols_io", "protocol"
        if "10.5281/zenodo" in path:
            return "zenodo", "data"
        if "10.31234/osf.io" in path:
            return "psyarxiv", "other"
        if "10.17605/osf.io" in path:
            return "osf", "data"
        return "doi", "data"
    return "other", "other"


def extract_artifact_links(text: str) -> list[ArtifactLink]:
    """Extract deduplicated repository, dataset, protocol, and preregistration links."""
    seen: set[str] = set()
    links: list[ArtifactLink] = []

    for match in _URL_RE.findall(text):
        url = _clean_match(match)
        if url in seen:
            continue
        host, kind = classify_url(url)
        if host == "other":
            continue
        seen.add(url)
        links.append(ArtifactLink(url=url, host=host, kind=kind))

    for doi_match in _DOI_RE.findall(text):
        doi = _clean_match(doi_match)
        doi_url = f"https://doi.org/{doi}"
        if doi_url in seen or any(doi in existing for existing in seen):
            continue
        host, kind = classify_url(doi_url)
        seen.add(doi_url)
        links.append(ArtifactLink(url=doi_url, host=host, kind=kind, metadata={"doi": doi}))

    return links


def scan_workspace_files(root: Path) -> tuple[list[str], list[str], list[str], list[str]]:
    """Classify files under a directory into (code, data, manifests, readmes)."""
    code_files: list[str] = []
    data_files: list[str] = []
    manifest_files: list[str] = []
    readme_files: list[str] = []

    if not root.exists():
        return code_files, data_files, manifest_files, readme_files

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part.startswith(".git") for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        name_lower = path.name.lower()
        suffix_lower = path.suffix.lower()

        if name_lower.startswith("readme"):
            readme_files.append(rel)
        if name_lower in _MANIFEST_NAMES:
            manifest_files.append(rel)
        if suffix_lower in _CODE_EXTENSIONS:
            code_files.append(rel)
        elif suffix_lower in _DATA_EXTENSIONS or name_lower.endswith((".csv.gz", ".tsv.gz")):
            data_files.append(rel)

    return code_files, data_files, manifest_files, readme_files
