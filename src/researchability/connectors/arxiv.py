"""arXiv connector: Atom API search, LaTeX/PDF parsing, and PaperBundle normalization."""

from __future__ import annotations

import gzip
import io
import re
import tarfile
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as ET
from pydantic import BaseModel, Field
from pypdf import PdfReader

from researchability.connectors.base import HttpConnector
from researchability.connectors.links import extract_artifact_links
from researchability.models import ArtifactManifest, FieldProfile, PaperBundle

_ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
_LATEX_SECTION_RE = re.compile(r"\\section\*?\{([^}]+)\}")
_LATEX_TABLE_RE = re.compile(r"\\begin\{table\*?\}(.*?)\\end\{table\*?\}", re.DOTALL)
_LATEX_CMD_URL_RE = re.compile(r"\\(?:url|href)\{([^}]+)\}")


class ArxivRecord(BaseModel):
    """Metadata record parsed from the arXiv Atom feed."""

    arxiv_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    abstract: str = ""
    published_year: int | None = None
    primary_category: str = ""
    categories: list[str] = Field(default_factory=list)
    doi: str | None = None
    pdf_url: str | None = None
    source_url: str | None = None


def normalize_arxiv_id(raw_id: str) -> str:
    """Strip URL prefixes or version suffixes into canonical arXiv ID."""
    cleaned = raw_id.strip()
    for prefix in ("https://arxiv.org/abs/", "http://arxiv.org/abs/", "arxiv:"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    return cleaned.rstrip("/")


def infer_profile_from_categories(categories: list[str]) -> FieldProfile:
    """Map arXiv subject categories onto a Researchability FieldProfile."""
    for cat in categories:
        if cat.startswith("q-bio"):
            return FieldProfile.BIOINFORMATICS
        if cat.startswith("econ.") or cat.startswith("q-fin."):
            return FieldProfile.ECONOMICS
    return FieldProfile.ML_CS


def _parse_atom_feed(xml_text: str) -> list[ArxivRecord]:
    root = ET.fromstring(xml_text)
    records: list[ArxivRecord] = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        id_text = entry.findtext("atom:id", default="", namespaces=_ATOM_NS)
        arxiv_id = normalize_arxiv_id(id_text.rsplit("/abs/", maxsplit=1)[-1])
        if not arxiv_id:
            continue
        title = " ".join(entry.findtext("atom:title", default="", namespaces=_ATOM_NS).split())
        abstract = " ".join(entry.findtext("atom:summary", default="", namespaces=_ATOM_NS).split())
        authors = [
            " ".join(a.findtext("atom:name", default="", namespaces=_ATOM_NS).split())
            for a in entry.findall("atom:author", _ATOM_NS)
        ]
        published = entry.findtext("atom:published", default="", namespaces=_ATOM_NS)
        published_year = (
            int(published[:4]) if len(published) >= 4 and published[:4].isdigit() else None
        )
        primary_el = entry.find("arxiv:primary_category", _ATOM_NS)
        primary_cat = primary_el.attrib.get("term", "") if primary_el is not None else ""
        categories = [c.attrib.get("term", "") for c in entry.findall("atom:category", _ATOM_NS)]
        doi = entry.findtext("arxiv:doi", default=None, namespaces=_ATOM_NS)
        pdf_url: str | None = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        for link_el in entry.findall("atom:link", _ATOM_NS):
            if link_el.attrib.get("title") == "pdf" and link_el.attrib.get("href"):
                pdf_url = link_el.attrib["href"]
        records.append(
            ArxivRecord(
                arxiv_id=arxiv_id,
                title=title,
                authors=[a for a in authors if a],
                abstract=abstract,
                published_year=published_year,
                primary_category=primary_cat,
                categories=[c for c in categories if c],
                doi=doi,
                pdf_url=pdf_url,
                source_url=f"https://arxiv.org/e-print/{arxiv_id}",
            )
        )
    return records


def _extract_latex_from_bytes(
    payload: bytes, target_dir: Path | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Extract concatenated .tex text and raw table environments from tar.gz or gzip/plain tex."""
    tex_chunks: list[str] = []
    if target_dir is not None:
        target_dir.mkdir(parents=True, exist_ok=True)

    # Attempt tar archive (gzipped or plain)
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                if member.name.endswith(".tex"):
                    extracted = tf.extractfile(member)
                    if extracted is not None:
                        tex_chunks.append(extracted.read().decode("utf-8", errors="replace"))
                if target_dir is not None:
                    safe_name = Path(member.name).name
                    if safe_name and not safe_name.startswith("."):
                        dest = target_dir / safe_name
                        extracted = tf.extractfile(member)
                        if extracted is not None:
                            dest.write_bytes(extracted.read())
    except tarfile.TarError:
        # Single gzipped .tex or plain .tex file
        try:
            decompressed = gzip.decompress(payload).decode("utf-8", errors="replace")
            tex_chunks.append(decompressed)
        except OSError:
            decoded = payload.decode("utf-8", errors="replace")
            if "\\documentclass" in decoded or "\\section" in decoded:
                tex_chunks.append(decoded)

    combined = "\n\n".join(tex_chunks)
    tables = [
        {"format": "latex", "raw": match.group(0)} for match in _LATEX_TABLE_RE.finditer(combined)
    ]
    return combined, tables


def _split_sections_from_latex(tex: str) -> dict[str, str]:
    """Split LaTeX source into normalized lowercase section buckets."""
    matches = list(_LATEX_SECTION_RE.finditer(tex))
    if not matches:
        return {}
    sections: dict[str, str] = {}
    for idx, match in enumerate(matches):
        title = match.group(1).strip().lower()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(tex)
        sections[title] = tex[start:end].strip()
    return sections


def _extract_pdf_text(payload: bytes) -> str:
    """Extract plain text from a PDF byte buffer via pypdf."""
    reader = PdfReader(io.BytesIO(payload))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages).strip()


class ArxivConnector(HttpConnector):
    """Client for searching arXiv and building normalized PaperBundle objects."""

    API_URL = "https://export.arxiv.org/api/query"

    def search(
        self,
        *,
        query: str = "",
        categories: list[str] | None = None,
        start: int = 0,
        max_results: int = 10,
    ) -> list[ArxivRecord]:
        """Search arXiv via the Atom API."""
        clauses: list[str] = []
        if query:
            clauses.append(f"all:{query}")
        if categories:
            cat_expr = " OR ".join(f"cat:{c}" for c in categories)
            clauses.append(f"({cat_expr})")
        search_query = " AND ".join(clauses) if clauses else "cat:cs.LG"

        resp = self.client.get(
            self.API_URL,
            params={
                "search_query": search_query,
                "start": start,
                "max_results": max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
        )
        resp.raise_for_status()
        return _parse_atom_feed(resp.text)

    def fetch_metadata(self, arxiv_id: str) -> ArxivRecord:
        """Fetch a single arXiv metadata record by ID."""
        canonical = normalize_arxiv_id(arxiv_id)
        resp = self.client.get(self.API_URL, params={"id_list": canonical})
        resp.raise_for_status()
        records = _parse_atom_feed(resp.text)
        if not records:
            raise ValueError(f"arXiv record not found: {canonical}")
        return records[0]

    def fetch_paper(
        self,
        arxiv_id: str,
        *,
        workdir: Path | None = None,
        prefer_latex: bool = True,
        fallback_pdf: bool = True,
    ) -> PaperBundle:
        """Fetch metadata, full text (LaTeX preferred, PDF fallback), and outbound links."""
        record = self.fetch_metadata(arxiv_id)
        full_text = ""
        sections: dict[str, str] = {}
        tables: list[dict[str, Any]] = []
        text_source = "abstract_only"

        if prefer_latex and record.source_url:
            src_resp = self.client.get(record.source_url)
            if src_resp.status_code == 200 and src_resp.content:
                tex, tex_tables = _extract_latex_from_bytes(
                    src_resp.content,
                    target_dir=(workdir / "latex_source") if workdir else None,
                )
                if tex.strip():
                    full_text = tex
                    sections = _split_sections_from_latex(tex)
                    tables = tex_tables
                    text_source = "latex"

        if not full_text and fallback_pdf and record.pdf_url:
            pdf_resp = self.client.get(record.pdf_url)
            if pdf_resp.status_code == 200 and pdf_resp.content:
                pdf_text = _extract_pdf_text(pdf_resp.content)
                if pdf_text:
                    full_text = pdf_text
                    text_source = "pdf"

        if not full_text:
            full_text = record.abstract

        # Extract URLs from \url{} / \href{} commands plus plain URLs/DOIs
        cmd_urls = "\n".join(_LATEX_CMD_URL_RE.findall(full_text))
        links = extract_artifact_links(f"{record.abstract}\n{full_text}\n{cmd_urls}")

        return PaperBundle(
            paper_id=f"arxiv:{record.arxiv_id}",
            source_repo="arxiv",
            title=record.title,
            authors=record.authors,
            published_year=record.published_year,
            doi=record.doi,
            primary_profile=infer_profile_from_categories(record.categories),
            abstract=record.abstract,
            full_text=full_text,
            sections=sections,
            tables=tables,
            artifacts=ArtifactManifest(links=links),
            metadata={
                "arxiv_id": record.arxiv_id,
                "categories": record.categories,
                "primary_category": record.primary_category,
                "text_source": text_source,
            },
        )
