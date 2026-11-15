"""Cross-field repository connectors for OSF/PsyArXiv (psychology/wet-lab) and ICPSR (economics)."""

from __future__ import annotations

import re
from typing import Any

import httpx

from researchability.connectors.base import HttpConnector
from researchability.connectors.links import extract_artifact_links
from researchability.models import (
    ArtifactLink,
    ArtifactManifest,
    ArtifactStatus,
    FieldProfile,
    PaperBundle,
)

_OSF_NODE_RE = re.compile(r"osf\.io/(?:registrations/)?([a-z0-9]{5})", re.IGNORECASE)
_PSYARXIV_RE = re.compile(r"(?:psyarxiv\.com|10\.31234/osf\.io)/([a-z0-9]{5})", re.IGNORECASE)
_ICPSR_ID_RE = re.compile(
    r"(?:10\.3886/(?:E|ICPSR)?(\d+)|(?:open)?icpsr[^0-9]*(\d+))",
    re.IGNORECASE,
)


def _extract_osf_node_id(identifier: str) -> str:
    cleaned = identifier.strip()
    m = _PSYARXIV_RE.search(cleaned) or _OSF_NODE_RE.search(cleaned)
    if m:
        return m.group(1).lower()
    if re.fullmatch(r"[a-z0-9]{5}", cleaned, re.IGNORECASE):
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
    ) -> PaperBundle:
        """Fetch an OSF node, registration, or PsyArXiv preprint metadata bundle."""
        node_id = _extract_osf_node_id(identifier)
        endpoint = (
            f"{self.base_url}/preprints/{node_id}/"
            if is_preprint or "psyarxiv" in identifier.lower() or "10.31234" in identifier.lower()
            else f"{self.base_url}/nodes/{node_id}/"
        )
        resp = self.client.get(endpoint)
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
        tags = attrs.get("tags") or []

        links = extract_artifact_links(desc)
        self_link = ArtifactLink(
            url=f"https://osf.io/{node_id}/",
            kind="prereg" if is_registration else "data",
            host="osf",
            status=ArtifactStatus.RESOLVED,
        )
        if not any(existing.url == self_link.url for existing in links):
            links.insert(0, self_link)

        return PaperBundle(
            paper_id=f"osf:{node_id}",
            source_repo="osf",
            title=title,
            published_year=year,
            doi=f"10.17605/OSF.IO/{node_id.upper()}",
            primary_profile=profile,
            abstract=desc,
            full_text=desc,
            artifacts=ArtifactManifest(links=links),
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

    def fetch_project(self, identifier: str) -> PaperBundle:
        """Fetch public OpenICPSR metadata JSON and build an Economics PaperBundle."""
        project_id = _extract_icpsr_id(identifier)
        meta_url = f"{self.base_url}/{project_id}/version/V1/view"
        resp = self.client.get(meta_url, headers={"Accept": "application/json"})
        resp.raise_for_status()

        raw_text = resp.text
        title = f"OpenICPSR Project {project_id}"
        description = ""
        files: list[str] = []
        year: int | None = None
        restricted = False

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
            url=f"https://doi.org/10.3886/E{project_id}V1",
            kind="data",
            host="icpsr",
            status=ArtifactStatus.BLOCKED_ACCESS if restricted else ArtifactStatus.RESOLVED,
        )
        return PaperBundle(
            paper_id=f"icpsr:{project_id}",
            source_repo="icpsr",
            title=title,
            published_year=year,
            doi=f"10.3886/E{project_id}V1",
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
