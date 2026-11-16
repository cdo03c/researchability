"""Tests for arXiv, Harvard Dataverse, link extraction, and outbound artifact resolution."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import httpx
from pypdf import PdfWriter

from researchability.connectors import (
    ArtifactResolver,
    ArxivConnector,
    DataverseConnector,
    extract_artifact_links,
)
from researchability.models import ArtifactLink, ArtifactManifest, ArtifactStatus, FieldProfile

_SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2501.00042v1</id>
    <published>2025-01-03T12:00:00Z</published>
    <title>Reproducible Single-Cell Benchmark</title>
    <summary>
      Code at https://github.com/example/sc-bench and data at 10.5281/zenodo.123456.
    </summary>
    <author><name>Ada Lovelace</name></author>
    <arxiv:primary_category term="q-bio.QM" />
    <category term="q-bio.QM" />
    <category term="cs.LG" />
    <link
      title="pdf"
      href="https://arxiv.org/pdf/2501.00042v1.pdf"
      rel="related"
      type="application/pdf"
    />
  </entry>
</feed>
"""


def _make_latex_tar_gz(tex_body: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = tex_body.encode("utf-8")
        info = tarfile.TarInfo(name="main.tex")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_blank_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_extract_artifact_links_classifies_hosts_and_dois() -> None:
    text = (
        "Code: https://github.com/org/repo.git, "
        "Data: https://huggingface.co/datasets/org/ds, "
        "DOI: 10.7910/DVN/ABC123."
    )
    links = extract_artifact_links(text)
    by_host = {(link.host, link.kind): link.url for link in links}

    assert ("github", "code") in by_host
    assert ("huggingface", "data") in by_host
    assert ("dataverse", "data") in by_host


def test_arxiv_search_and_latex_ingestion(tmp_path: Path) -> None:
    tex = (
        r"\section{Introduction} We study cell lines."
        r"\section{Methods} Code is at \url{https://github.com/example/sc-bench}."
        r"\section{Results} \begin{table}Accuracy 92.1\%\end{table}"
    )
    tar_bytes = _make_latex_tar_gz(tex)

    def handler(request: httpx.Request) -> httpx.Response:
        if "export.arxiv.org/api/query" in str(request.url):
            return httpx.Response(200, text=_SAMPLE_ATOM)
        if "e-print" in str(request.url):
            return httpx.Response(200, content=tar_bytes)
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with ArxivConnector(http_client=http) as arxiv:
        hits = arxiv.search(categories=["q-bio.QM"], max_results=1)
        assert len(hits) == 1
        assert hits[0].arxiv_id == "2501.00042v1"

        bundle = arxiv.fetch_paper("2501.00042v1", workdir=tmp_path)

    assert bundle.paper_id == "arxiv:2501.00042v1"
    assert bundle.primary_profile == FieldProfile.BIOINFORMATICS
    assert bundle.metadata["text_source"] == "latex"
    assert "methods" in bundle.sections
    assert len(bundle.tables) == 1
    assert any(link.host == "github" for link in bundle.artifacts.links)
    assert any(link.host == "zenodo" for link in bundle.artifacts.links)


def test_arxiv_falls_back_to_pdf_when_latex_unavailable() -> None:
    pdf_bytes = _make_blank_pdf_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if "export.arxiv.org/api/query" in str(request.url):
            return httpx.Response(200, text=_SAMPLE_ATOM)
        if "e-print" in str(request.url):
            return httpx.Response(404)
        if "pdf" in str(request.url):
            return httpx.Response(200, content=pdf_bytes)
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with ArxivConnector(http_client=http) as arxiv:
        bundle = arxiv.fetch_paper("2501.00042v1")

    # Blank PDF yields empty page text, so connector falls back cleanly to abstract
    assert bundle.paper_id == "arxiv:2501.00042v1"
    assert "Code at https://github.com/example/sc-bench" in bundle.full_text


def test_dataverse_fetch_downloads_and_classifies_files(tmp_path: Path) -> None:
    dataset_json = {
        "data": {
            "publicationDate": "2024-06-01",
            "latestVersion": {
                "releaseTime": "2024-06-01T00:00:00Z",
                "metadataBlocks": {
                    "citation": {
                        "fields": [
                            {"typeName": "title", "value": "ML Replication Package"},
                            {"typeName": "subject", "value": ["Computer and Information Science"]},
                            {
                                "typeName": "author",
                                "value": [{"authorName": {"value": "Grace Hopper"}}],
                            },
                            {
                                "typeName": "dsDescription",
                                "value": [{"dsDescriptionValue": {"value": "Includes prep.py"}}],
                            },
                        ]
                    }
                },
                "files": [
                    {
                        "label": "README.md",
                        "restricted": False,
                        "dataFile": {"id": 1, "filesize": 20},
                    },
                    {
                        "label": "prep.py",
                        "restricted": False,
                        "dataFile": {"id": 2, "filesize": 30},
                    },
                    {
                        "label": "cohort.csv",
                        "restricted": False,
                        "dataFile": {"id": 3, "filesize": 40},
                    },
                ],
            },
        }
    }
    search_json = {
        "data": {
            "items": [{"name": "ML Replication Package", "global_id": "doi:10.7910/DVN/TEST1"}]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/search" in url:
            return httpx.Response(200, json=search_json)
        if "/api/datasets/:persistentId/" in url:
            return httpx.Response(200, json=dataset_json)
        if "/api/access/datafile/1" in url:
            return httpx.Response(200, text="Run python prep.py")
        if "/api/access/datafile/2" in url:
            return httpx.Response(200, text="print('prep')")
        if "/api/access/datafile/3" in url:
            return httpx.Response(200, text="id,y\n1,0\n")
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with DataverseConnector(http_client=http) as dv:
        hits = dv.search(query="replication")
        assert len(hits) == 1
        bundle = dv.fetch_dataset("doi:10.7910/DVN/TEST1", workdir=tmp_path)

    assert bundle.paper_id == "dataverse:doi:10.7910/DVN/TEST1"
    assert bundle.authors == ["Grace Hopper"]
    assert "prep.py" in bundle.artifacts.code_files
    assert "cohort.csv" in bundle.artifacts.data_files
    assert "README.md" in bundle.artifacts.readme_files
    assert bundle.artifacts.links[0].status == ArtifactStatus.RESOLVED


def test_dataverse_flags_restricted_and_oversized_as_blocked() -> None:
    def make_payload(restricted: bool, size: int) -> dict:
        return {
            "data": {
                "latestVersion": {
                    "metadataBlocks": {"citation": {"fields": []}},
                    "files": [
                        {
                            "label": "secret.csv",
                            "restricted": restricted,
                            "dataFile": {"id": 9, "filesize": size},
                        }
                    ],
                }
            }
        }

    def handler(request: httpx.Request) -> httpx.Response:
        pid = request.url.params.get("persistentId")
        if pid == "doi:10.7910/DVN/RESTRICTED":
            return httpx.Response(200, json=make_payload(True, 100))
        return httpx.Response(200, json=make_payload(False, 5_000_000_000))

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with DataverseConnector(http_client=http) as dv:
        restricted_bundle = dv.fetch_dataset("doi:10.7910/DVN/RESTRICTED", download_files=False)
        oversized_bundle = dv.fetch_dataset(
            "doi:10.7910/DVN/HUGE",
            download_files=False,
            max_total_bytes=1024,
        )

    assert restricted_bundle.artifacts.links[0].status == ArtifactStatus.BLOCKED_ACCESS
    assert oversized_bundle.artifacts.links[0].status == ArtifactStatus.BLOCKED_DATA_SIZE


def test_artifact_resolver_classifies_github_zenodo_and_osf(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://api.github.com/repos/org/good-repo":
            return httpx.Response(200, json={"full_name": "org/good-repo", "size": 50})
        if url == "https://api.github.com/repos/org/stub-repo":
            return httpx.Response(200, json={"full_name": "org/stub-repo", "size": 2})
        if url == "https://api.github.com/repos/org/missing":
            return httpx.Response(404)
        if url == "https://zenodo.org/api/records/123456":
            return httpx.Response(
                200,
                json={"metadata": {"access_right": "open"}, "files": [{"size": 1000}]},
            )
        if url == "https://api.osf.io/v2/nodes/abcde/":
            return httpx.Response(200, json={"data": {"attributes": {"public": False}}})
        return httpx.Response(404)

    def fake_clone(repo_url: str, dest: Path) -> bool:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "README.md").write_text("# Repo", encoding="utf-8")
        if "good-repo" in repo_url:
            (dest / "run.py").write_text("print(1)", encoding="utf-8")
            (dest / "requirements.txt").write_text("numpy==2.0.0", encoding="utf-8")
        return True

    manifest = ArtifactManifest(
        links=[
            ArtifactLink(url="https://github.com/org/good-repo", host="github", kind="code"),
            ArtifactLink(url="https://github.com/org/stub-repo", host="github", kind="code"),
            ArtifactLink(url="https://github.com/org/missing", host="github", kind="code"),
            ArtifactLink(url="https://zenodo.org/records/123456", host="zenodo", kind="data"),
            ArtifactLink(url="https://osf.io/abcde/", host="osf", kind="data"),
        ]
    )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with ArtifactResolver(http_client=http, clone_runner=fake_clone) as resolver:
        resolved = resolver.resolve_manifest(manifest, workdir=tmp_path)

    statuses = {link.url: link.status for link in resolved.links}
    assert statuses["https://github.com/org/good-repo"] == ArtifactStatus.RESOLVED
    assert statuses["https://github.com/org/stub-repo"] == ArtifactStatus.STUB_ONLY
    assert statuses["https://github.com/org/missing"] == ArtifactStatus.BROKEN_404
    assert statuses["https://zenodo.org/records/123456"] == ArtifactStatus.RESOLVED
    assert statuses["https://osf.io/abcde/"] == ArtifactStatus.BLOCKED_ACCESS
    assert "run.py" in resolved.code_files
    assert "requirements.txt" in resolved.manifest_files
    # Ensure JSON serialization round-trips cleanly
    assert json.loads(resolved.model_dump_json())["code_files"] == ["run.py"]
