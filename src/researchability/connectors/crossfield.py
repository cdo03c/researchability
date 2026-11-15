"""Cross-field repository connectors for OSF/PsyArXiv (psychology/wet-lab) and ICPSR (economics)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx

from researchability.connectors.base import HttpConnector, download_osf_storage_files
from researchability.connectors.links import (
    classify_relative_paths,
    extract_artifact_links,
    scan_workspace_files,
)
from researchability.models import (
    ArtifactLink,
    ArtifactManifest,
    ArtifactStatus,
    FieldProfile,
    PaperBundle,
)

_OSF_NODE_RE = re.compile(
    r"osf\.io/(?:registrations/|preprints/(?:[a-z]+/)?)?([a-z0-9]{5}(?:_v\d+)?)",
    re.IGNORECASE,
)
_PSYARXIV_RE = re.compile(
    r"(?:psyarxiv\.com|10\.31234/osf\.io)/([a-z0-9]{5}(?:_v\d+)?)",
    re.IGNORECASE,
)
_ICPSR_ID_RE = re.compile(
    r"(?:10\.3886/(?:E|ICPSR)?(\d+)|(?:open)?icpsr[^0-9]*(\d+))",
    re.IGNORECASE,
)


def _extract_osf_node_id(identifier: str) -> str:
    cleaned = identifier.strip()
    m = _PSYARXIV_RE.search(cleaned) or _OSF_NODE_RE.search(cleaned)
    if m:
        return m.group(1).lower()
    if re.fullmatch(r"[a-z0-9]{5}(?:_v\d+)?", cleaned, re.IGNORECASE):
        return cleaned.lower()
    msg = f"Cannot parse OSF/PsyArXiv node ID from '{identifier}'."
    raise ValueError(msg)


def _extract_icpsr_id(identifier: str) -> str:
    cleaned = identifier.strip()
    m = _ICPSR_ID_RE.search(cleaned)
    if m:
        return m.group(1) or m.group(2)
    digits = re.sub(r"[^0-9]", "", cleaned)
    if digits:
        return digits
    msg = f"Cannot parse ICPSR/OpenICPSR project ID from '{identifier}'."
    raise ValueError(msg)


class OsfConnector(HttpConnector):
    """Fetch OSF project, registration, or PsyArXiv preprint metadata into a PaperBundle."""

    API_BASE = "https://api.osf.io/v2"

    def __init__(
        self,
        base_url: str = API_BASE,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(http_client=http_client, timeout=timeout)
        self.base_url = base_url.rstrip("/")

    def fetch_project(
        self,
        identifier: str,
        *,
        is_preprint: bool = False,
        profile: FieldProfile = FieldProfile.PSYCHOLOGY_SOCIAL,
        workdir: Path | None = None,
        download_files: bool = False,
        max_total_bytes: int = 250 * 1024 * 1024,
    ) -> PaperBundle:
        """Fetch an OSF node, registration, or PsyArXiv preprint bundle (and optional files)."""
        node_id = _extract_osf_node_id(identifier)
        lowered = identifier.lower()
        is_psyarxiv = is_preprint or "psyarxiv" in lowered or "10.31234" in lowered
        collection = "nodes"
        if is_psyarxiv:
            endpoint = f"{self.base_url}/preprints/{node_id}/"
            resp = self.client.get(endpoint)
        elif "registrations" in lowered:
            collection = "registrations"
            endpoint = f"{self.base_url}/registrations/{node_id}/"
            resp = self.client.get(endpoint)
        else:
            resp = self.client.get(f"{self.base_url}/nodes/{node_id}/")
            if resp.status_code == 404:
                reg_resp = self.client.get(f"{self.base_url}/registrations/{node_id}/")
                if reg_resp.status_code < 400:
                    resp = reg_resp
                    collection = "registrations"
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json().get("data", {})
        attrs: dict[str, Any] = payload.get("attributes", {})

        title = str(attrs.get("title") or node_id)
        desc = str(attrs.get("description") or "")
        date_created = str(attrs.get("date_created") or "")
        year = (
            int(date_created[:4]) if len(date_created) >= 4 and date_created[:4].isdigit() else None
        )
        is_registration = bool(attrs.get("registration")) or attrs.get("category") == "registration"
        if is_registration and collection == "nodes":
            collection = "registrations"
        is_public = bool(attrs.get("public", True))
        tags = attrs.get("tags") or []

        links = extract_artifact_links(desc)
        local_dir = (workdir / "osf_files" / node_id) if (download_files and workdir) else None
        link_status = ArtifactStatus.RESOLVED if is_public else ArtifactStatus.BLOCKED_ACCESS
        code_files: list[str] = []
        data_files: list[str] = []
        manifest_files: list[str] = []
        readme_files: list[str] = []
        total_bytes = 0

        if is_public and not is_psyarxiv:
            listing_url = f"{self.base_url}/{collection}/{node_id}/files/osfstorage/"
            discovered, total_bytes, exceeded = download_osf_storage_files(
                self.client,
                listing_url,
                local_dir or Path("osf_files"),
                max_bytes=max_total_bytes,
                download_files=local_dir is not None,
                resolve_dns=self._owns_client,
            )
            if exceeded:
                link_status = ArtifactStatus.BLOCKED_DATA_SIZE
            if local_dir is not None and local_dir.exists():
                code_files, data_files, manifest_files, readme_files = scan_workspace_files(
                    local_dir
                )
            elif discovered:
                code_files, data_files, manifest_files, readme_files = classify_relative_paths(
                    discovered
                )

        self_link = ArtifactLink(
            url=f"https://osf.io/{node_id}/",
            kind="prereg" if is_registration else "data",
            host="osf",
            status=link_status,
            local_path=str(local_dir) if (local_dir is not None and local_dir.exists()) else None,
            size_bytes=total_bytes,
        )
        if not any(existing.url == self_link.url for existing in links):
            links.insert(0, self_link)

        base_node_id = re.sub(r"_v\d+$", "", node_id, flags=re.IGNORECASE)
        default_doi = (
            f"10.31234/osf.io/{base_node_id}"
            if is_psyarxiv
            else f"10.17605/OSF.IO/{base_node_id.upper()}"
        )
        resolved_doi = str(attrs.get("preprint_doi") or default_doi)

        return PaperBundle(
            paper_id=f"osf:{node_id}",
            source_repo="osf",
            title=title,
            published_year=year,
            doi=resolved_doi,
            primary_profile=profile,
            abstract=desc,
            full_text=desc,
            artifacts=ArtifactManifest(
                links=links,
                code_files=code_files,
                data_files=data_files,
                manifest_files=manifest_files,
                readme_files=readme_files,
            ),
            metadata={
                "osf_node_id": node_id,
                "is_registration": is_registration,
                "tags": tags,
                "preregistration_disclosed": is_registration,
            },
        )


class IcpsrConnector(HttpConnector):
    """Client for OpenICPSR / ICPSR economics & social science replication archives."""

    OPENICPSR_BASE = "https://www.openicpsr.org/openicpsr/project"

    def __init__(
        self,
        base_url: str = OPENICPSR_BASE,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(http_client=http_client, timeout=timeout)
        self.base_url = base_url.rstrip("/")

    def _extract_version(self, identifier: str) -> str:
        ver_match = re.search(r"(?:version[/=]|E\d+)(V\d+)\b", identifier, re.IGNORECASE)
        return ver_match.group(1).upper() if ver_match else "V1"

    def _fetch_datacite_fallback(
        self,
        project_id: str,
        preferred_version: str = "V1",
    ) -> dict[str, Any] | None:
        """Query DataCite DOI API when OpenICPSR web endpoints block automated requests."""
        versions = list(dict.fromkeys([preferred_version, "V1", "V2", "V3"]))
        candidates = [
            *(f"10.3886/E{project_id}{v}" for v in versions),
            f"10.3886/ICPSR{project_id}",
        ]
        for candidate_doi in candidates:
            resp = self.client.get(f"https://api.datacite.org/dois/{candidate_doi}")
            if resp.status_code == 200:
                payload = resp.json()
                attrs = payload.get("data", {}).get("attributes", {})
                if isinstance(attrs, dict):
                    attrs = dict(attrs)
                    attrs["_resolved_doi"] = candidate_doi
                    return attrs
        return None

    def fetch_project(self, identifier: str) -> PaperBundle:
        """Fetch OpenICPSR metadata JSON, falling back to DataCite DOI metadata if gated."""
        project_id = _extract_icpsr_id(identifier)
        version = self._extract_version(identifier)
        meta_url = f"{self.base_url}/{project_id}/version/{version}/view"
        title = f"OpenICPSR Project {project_id}"
        description = ""
        files: list[str] = []
        year: int | None = None
        restricted = False
        doi = f"10.3886/E{project_id}{version}"

        try:
            resp = self.client.get(meta_url, headers={"Accept": "application/json"})
            if resp.status_code == 404 and version == "V1":
                v2_resp = self.client.get(
                    f"{self.base_url}/{project_id}/version/V2/view",
                    headers={"Accept": "application/json"},
                )
                if v2_resp.status_code < 400:
                    resp = v2_resp
                    version = "V2"
                    doi = f"10.3886/E{project_id}V2"
            resp.raise_for_status()
            raw_text = resp.text
            if "application/json" in resp.headers.get(
                "content-type", ""
            ) or raw_text.lstrip().startswith("{"):
                data: dict[str, Any] = resp.json()
                title = str(data.get("title") or title)
                description = str(data.get("summary") or data.get("description") or "")
                raw_files = data.get("files") or []
                if isinstance(raw_files, list):
                    files = [str(f) for f in raw_files]
                pub_date = str(data.get("publicationDate") or "")
                if len(pub_date) >= 4 and pub_date[:4].isdigit():
                    year = int(pub_date[:4])
                restricted = bool(data.get("restricted", False))
            else:
                # Parse minimal title & file references from landing payload
                m_title = re.search(r"<title>(.*?)</title>", raw_text, re.IGNORECASE | re.DOTALL)
                if m_title:
                    title = " ".join(m_title.group(1).split())
                description = raw_text[:4000]
                restricted = (
                    "restricted use" in raw_text.lower() or "apply for access" in raw_text.lower()
                )
        except httpx.HTTPError:
            dc = self._fetch_datacite_fallback(project_id, preferred_version=version)
            if dc is None:
                raise
            doi = str(dc.get("_resolved_doi") or doi)
            titles = dc.get("titles") or []
            if isinstance(titles, list) and titles:
                first_title = titles[0]
                if isinstance(first_title, dict) and first_title.get("title"):
                    title = str(first_title["title"])
            descs = dc.get("descriptions") or []
            if isinstance(descs, list):
                description = "\n\n".join(
                    str(d.get("description", ""))
                    for d in descs
                    if isinstance(d, dict) and d.get("description")
                )
            pub_year = dc.get("publicationYear")
            if isinstance(pub_year, int):
                year = pub_year
            rights_text = " ".join(
                str(r.get("rights", ""))
                for r in (dc.get("rightsList") or [])
                if isinstance(r, dict)
            ).lower()
            restricted = "restricted" in rights_text or "restricted" in description.lower()

        code_files = [
            f
            for f in files
            if f.lower().endswith((".do", ".ado", ".r", ".rmd", ".py", ".sas", ".sps"))
        ]
        data_files = [
            f for f in files if f.lower().endswith((".dta", ".csv", ".tsv", ".rds", ".sav"))
        ]
        readme_files = [f for f in files if f.lower().startswith("readme") or "readme" in f.lower()]

        link = ArtifactLink(
            url=f"https://doi.org/{doi}",
            kind="data",
            host="icpsr",
            status=ArtifactStatus.BLOCKED_ACCESS if restricted else ArtifactStatus.RESOLVED,
        )
        return PaperBundle(
            paper_id=f"icpsr:{project_id}",
            source_repo="icpsr",
            title=title,
            published_year=year,
            doi=doi,
            primary_profile=FieldProfile.ECONOMICS,
            abstract=description,
            full_text=description,
            artifacts=ArtifactManifest(
                links=[link],
                code_files=code_files,
                data_files=data_files,
                readme_files=readme_files,
            ),
            metadata={
                "icpsr_project_id": project_id,
                "restricted": restricted,
            },
        )
