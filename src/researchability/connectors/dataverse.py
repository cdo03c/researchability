"""Harvard Dataverse connector: Search/Dataset REST APIs and replication bundle downloader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from researchability.connectors.base import HttpConnector
from researchability.connectors.links import extract_artifact_links, scan_workspace_files
from researchability.models import (
    ArtifactLink,
    ArtifactManifest,
    ArtifactStatus,
    FieldProfile,
    PaperBundle,
)


class DataverseSearchHit(BaseModel):
    """Search result item returned by the Harvard Dataverse Search API."""

    name: str
    global_id: str
    url: str = ""
    published_at: str = ""
    description: str = ""
    subjects: list[str] = Field(default_factory=list)


def _infer_profile_from_subjects(subjects: list[str]) -> FieldProfile:
    lowered = [s.lower() for s in subjects]
    if any(
        "medicine" in s or "health" in s or "life sciences" in s or "biolog" in s for s in lowered
    ):
        return FieldProfile.BIOINFORMATICS
    if any("computer" in s or "engineering" in s or "mathematic" in s for s in lowered):
        return FieldProfile.ML_CS
    if any("social" in s or "economics" in s or "business" in s for s in lowered):
        return FieldProfile.ECONOMICS
    return FieldProfile.ML_CS


def _extract_citation_fields(latest_version: dict[str, Any]) -> dict[str, Any]:
    """Flatten Harvard Dataverse citation metadata block fields into a dict."""
    citation_block = latest_version.get("metadataBlocks", {}).get("citation", {})
    fields = citation_block.get("fields", [])
    parsed: dict[str, Any] = {}
    for field in fields:
        name = field.get("typeName")
        if name:
            parsed[name] = field.get("value")
    return parsed


def _parse_authors(raw_authors: Any) -> list[str]:
    if not isinstance(raw_authors, list):
        return []
    names: list[str] = []
    for item in raw_authors:
        if isinstance(item, dict):
            val = item.get("authorName", {}).get("value")
            if isinstance(val, str) and val.strip():
                names.append(val.strip())
    return names


def _parse_description(raw_desc: Any) -> str:
    if isinstance(raw_desc, str):
        return raw_desc
    if isinstance(raw_desc, list):
        parts: list[str] = []
        for item in raw_desc:
            if isinstance(item, dict):
                val = item.get("dsDescriptionValue", {}).get("value")
                if isinstance(val, str):
                    parts.append(val.strip())
        return "\n\n".join(parts)
    return ""


def _parse_publication_doi(raw_pubs: Any) -> str | None:
    if not isinstance(raw_pubs, list):
        return None
    for item in raw_pubs:
        if isinstance(item, dict):
            id_num = item.get("publicationIDNumber", {}).get("value")
            if isinstance(id_num, str) and id_num.strip():
                return id_num.strip()
    return None


class DataverseConnector(HttpConnector):
    """Client for searching Harvard Dataverse and downloading replication packages."""

    BASE_URL = "https://dataverse.harvard.edu"

    def __init__(
        self,
        base_url: str = BASE_URL,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(http_client=http_client, timeout=timeout)
        self.base_url = base_url.rstrip("/")

    def search(
        self,
        *,
        query: str = "*",
        subtree: str | None = None,
        per_page: int = 10,
        start: int = 0,
    ) -> list[DataverseSearchHit]:
        """Search Harvard Dataverse datasets."""
        params: dict[str, Any] = {
            "q": query,
            "type": "dataset",
            "per_page": per_page,
            "start": start,
        }
        if subtree:
            params["subtree"] = subtree
        resp = self.client.get(f"{self.base_url}/api/search", params=params)
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("items", [])
        return [
            DataverseSearchHit(
                name=item.get("name", ""),
                global_id=item.get("global_id", ""),
                url=item.get("url", ""),
                published_at=item.get("published_at", ""),
                description=item.get("description", ""),
                subjects=item.get("subjects", []) or [],
            )
            for item in items
            if item.get("global_id")
        ]

    def fetch_dataset(
        self,
        persistent_id: str,
        *,
        workdir: Path | None = None,
        download_files: bool = True,
        max_total_bytes: int = 250 * 1024 * 1024,
    ) -> PaperBundle:
        """Fetch dataset metadata, inspect file manifest, and optionally download files."""
        resp = self.client.get(
            f"{self.base_url}/api/datasets/:persistentId/",
            params={"persistentId": persistent_id},
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        latest = data.get("latestVersion", {})
        fields = _extract_citation_fields(latest)

        title = fields.get("title") or persistent_id
        authors = _parse_authors(fields.get("author"))
        abstract = _parse_description(fields.get("dsDescription"))
        raw_subjects = fields.get("subject")
        subjects: list[str] = (
            [str(s) for s in raw_subjects] if isinstance(raw_subjects, list) else []
        )
        pub_date = latest.get("releaseTime") or data.get("publicationDate") or ""
        published_year = (
            int(pub_date[:4]) if len(pub_date) >= 4 and pub_date[:4].isdigit() else None
        )
        related_doi = _parse_publication_doi(fields.get("publication"))

        files_raw: list[dict[str, Any]] = latest.get("files", [])
        total_bytes = sum(int(f.get("dataFile", {}).get("filesize", 0)) for f in files_raw)
        restricted_count = sum(1 for f in files_raw if f.get("restricted"))

        dataset_url = f"{self.base_url}/dataset.xhtml?persistentId={persistent_id}"
        dataset_status = ArtifactStatus.RESOLVED
        if restricted_count == len(files_raw) and files_raw:
            dataset_status = ArtifactStatus.BLOCKED_ACCESS
        elif total_bytes > max_total_bytes:
            dataset_status = ArtifactStatus.BLOCKED_DATA_SIZE

        local_dir = (workdir / "dataverse_files") if workdir else None
        downloaded_paths: list[str] = []

        if download_files and local_dir is not None and dataset_status == ArtifactStatus.RESOLVED:
            local_dir.mkdir(parents=True, exist_ok=True)
            for entry in files_raw:
                if entry.get("restricted"):
                    continue
                df = entry.get("dataFile", {})
                file_id = df.get("id")
                filename = entry.get("label") or df.get("filename")
                if not file_id or not filename:
                    continue
                dir_label = entry.get("directoryLabel", "").strip("/")
                dest = (
                    local_dir / dir_label / Path(filename).name
                    if dir_label
                    else local_dir / Path(filename).name
                )
                dest.parent.mkdir(parents=True, exist_ok=True)
                file_resp = self.client.get(f"{self.base_url}/api/access/datafile/{file_id}")
                if file_resp.status_code == 200:
                    dest.write_bytes(file_resp.content)
                    downloaded_paths.append(dest.relative_to(local_dir).as_posix())
                elif file_resp.status_code in {401, 403}:
                    dataset_status = ArtifactStatus.BLOCKED_ACCESS

        if local_dir is not None and local_dir.exists():
            code_files, data_files, manifest_files, readme_files = scan_workspace_files(local_dir)
        else:
            # Classify from remote manifest labels when download_files=False
            code_files, data_files, manifest_files, readme_files = _classify_remote_manifest(
                files_raw
            )

        readme_text = ""
        if local_dir is not None and readme_files:
            readme_path = local_dir / readme_files[0]
            if readme_path.exists():
                readme_text = readme_path.read_text(encoding="utf-8", errors="replace")

        links = [
            ArtifactLink(
                url=dataset_url,
                kind="data",
                host="dataverse",
                status=dataset_status,
                local_path=str(local_dir) if local_dir else None,
                size_bytes=total_bytes,
                metadata={
                    "file_count": len(files_raw),
                    "restricted_count": restricted_count,
                },
            ),
            *extract_artifact_links(f"{abstract}\n{readme_text}"),
        ]

        sections = {"readme": readme_text} if readme_text else {}
        full_text = f"{abstract}\n\n{readme_text}".strip()

        return PaperBundle(
            paper_id=f"dataverse:{persistent_id}",
            source_repo="dataverse",
            title=str(title),
            authors=authors,
            published_year=published_year,
            doi=related_doi or persistent_id,
            primary_profile=_infer_profile_from_subjects(subjects),
            abstract=abstract,
            full_text=full_text,
            sections=sections,
            artifacts=ArtifactManifest(
                links=links,
                code_files=code_files,
                data_files=data_files,
                manifest_files=manifest_files,
                readme_files=readme_files,
            ),
            metadata={
                "persistent_id": persistent_id,
                "subjects": subjects,
                "total_bytes": total_bytes,
                "restricted_file_count": restricted_count,
                "downloaded_files": downloaded_paths,
            },
        )


def _classify_remote_manifest(
    files_raw: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Classify Dataverse file entries by filename without downloading bytes."""
    code_files: list[str] = []
    data_files: list[str] = []
    manifest_files: list[str] = []
    readme_files: list[str] = []

    for entry in files_raw:
        df = entry.get("dataFile", {})
        label = entry.get("label") or df.get("filename") or ""
        dir_label = entry.get("directoryLabel", "").strip("/")
        rel = f"{dir_label}/{label}" if dir_label else label
        lower = Path(label).name.lower()
        suffix = Path(label).suffix.lower()

        if lower.startswith("readme"):
            readme_files.append(rel)
        if lower in {
            "pyproject.toml",
            "requirements.txt",
            "environment.yml",
            "renv.lock",
            "dockerfile",
        }:
            manifest_files.append(rel)
        if suffix in {".py", ".r", ".rmd", ".ipynb", ".sh", ".do", ".ado", ".jl", ".m"}:
            code_files.append(rel)
        elif suffix in {
            ".csv",
            ".tsv",
            ".tab",
            ".parquet",
            ".json",
            ".rds",
            ".dta",
            ".sav",
            ".h5ad",
            ".xlsx",
        }:
            data_files.append(rel)

    return code_files, data_files, manifest_files, readme_files
