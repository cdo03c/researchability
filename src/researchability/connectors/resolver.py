"""Outbound artifact resolver for GitHub, Zenodo, OSF, Hugging Face, and DOIs."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from researchability.connectors.base import HttpConnector, UnsafeUrlError
from researchability.connectors.links import is_safe_public_url, scan_workspace_files
from researchability.models import ArtifactLink, ArtifactManifest, ArtifactStatus, PaperBundle

_ZENODO_ID_RE = re.compile(r"zenodo\.(?:org/records?|org/record)/(\d+)|10\.5281/zenodo\.(\d+)")
_OSF_ID_RE = re.compile(r"osf\.io/([A-Za-z0-9]{5,})")
_GITHUB_REPO_RE = re.compile(r"^/([^/]+)/([^/]+?)(?:\.git)?(?:/.*)?$")

CloneRunner = Callable[[str, Path], bool]


def _default_git_clone(repo_url: str, dest: Path) -> bool:
    """Perform a shallow git clone without interactive prompts."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    safe_env = {"GIT_TERMINAL_PROMPT": "0"}
    for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if key in os.environ:
            safe_env[key] = os.environ[key]
    safe_env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, str(dest)],
        env=safe_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return proc.returncode == 0


class ArtifactResolver(HttpConnector):
    """Resolve outbound code and data links and classify availability & stub status."""

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        clone_runner: CloneRunner | None = None,
        max_data_bytes: int = 1_000_000_000,
        timeout: float = 20.0,
    ) -> None:
        super().__init__(http_client=http_client, timeout=timeout)
        self.clone_runner = clone_runner or _default_git_clone
        self.max_data_bytes = max_data_bytes

    def _resolve_github(
        self,
        link: ArtifactLink,
        workdir: Path | None,
    ) -> tuple[ArtifactLink, list[str], list[str], list[str], list[str]]:
        parsed = urlparse(link.url)
        match = _GITHUB_REPO_RE.match(parsed.path)
        if not match:
            return link.model_copy(update={"status": ArtifactStatus.BROKEN_404}), [], [], [], []

        owner, repo = match.group(1), match.group(2)
        canonical_url = f"https://github.com/{owner}/{repo}"
        api_url = f"https://api.github.com/repos/{owner}/{repo}"
        headers = {"Accept": "application/vnd.github+json"}
        gh_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
        if gh_token:
            headers["Authorization"] = f"Bearer {gh_token}"
        resp = self.client.get(api_url, headers=headers)

        if resp.status_code == 404:
            return link.model_copy(update={"status": ArtifactStatus.BROKEN_404}), [], [], [], []
        if resp.status_code == 403 and (
            resp.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in resp.text.lower()
        ):
            return (
                link.model_copy(
                    update={
                        "status": ArtifactStatus.UNRESOLVED,
                        "metadata": {**link.metadata, "rate_limited": True},
                    }
                ),
                [],
                [],
                [],
                [],
            )
        if resp.status_code in {401, 403}:
            return link.model_copy(update={"status": ArtifactStatus.BLOCKED_ACCESS}), [], [], [], []
        if resp.status_code >= 400:
            return link.model_copy(update={"status": ArtifactStatus.UNRESOLVED}), [], [], [], []

        repo_meta = resp.json()
        size_kb = int(repo_meta.get("size", 0))
        meta = {
            **link.metadata,
            "full_name": repo_meta.get("full_name", f"{owner}/{repo}"),
            "default_branch": repo_meta.get("default_branch", "main"),
            "size_kb": size_kb,
        }

        if workdir is None:
            status = ArtifactStatus.STUB_ONLY if size_kb == 0 else ArtifactStatus.RESOLVED
            return link.model_copy(update={"status": status, "metadata": meta}), [], [], [], []

        clone_dir = workdir / "repos" / f"{owner}__{repo}"
        if not clone_dir.exists():
            cloned = self.clone_runner(f"{canonical_url}.git", clone_dir)
            if not cloned:
                return (
                    link.model_copy(update={"status": ArtifactStatus.UNRESOLVED, "metadata": meta}),
                    [],
                    [],
                    [],
                    [],
                )

        code_files, data_files, manifest_files, readme_files = scan_workspace_files(clone_dir)
        status = ArtifactStatus.RESOLVED if code_files else ArtifactStatus.STUB_ONLY
        updated = link.model_copy(
            update={
                "status": status,
                "local_path": str(clone_dir),
                "size_bytes": size_kb * 1024,
                "metadata": meta,
            }
        )
        return updated, code_files, data_files, manifest_files, readme_files

    def _resolve_zenodo(self, link: ArtifactLink) -> ArtifactLink:
        match = _ZENODO_ID_RE.search(link.url)
        if not match:
            return self._resolve_http_head(link)
        record_id = match.group(1) or match.group(2)
        resp = self.client.get(f"https://zenodo.org/api/records/{record_id}")
        if resp.status_code == 404:
            return link.model_copy(update={"status": ArtifactStatus.BROKEN_404})
        if resp.status_code in {401, 403}:
            return link.model_copy(update={"status": ArtifactStatus.BLOCKED_ACCESS})
        if resp.status_code >= 400:
            return link.model_copy(update={"status": ArtifactStatus.UNRESOLVED})

        data = resp.json()
        access_right = data.get("metadata", {}).get("access_right", "open")
        files = data.get("files", [])
        total_bytes = sum(int(f.get("size", 0)) for f in files)
        if access_right in {"restricted", "embargoed", "closed"}:
            status = ArtifactStatus.BLOCKED_ACCESS
        elif total_bytes > self.max_data_bytes:
            status = ArtifactStatus.BLOCKED_DATA_SIZE
        else:
            status = ArtifactStatus.RESOLVED
        return link.model_copy(
            update={
                "status": status,
                "size_bytes": total_bytes,
                "metadata": {
                    **link.metadata,
                    "zenodo_id": record_id,
                    "access_right": access_right,
                    "file_count": len(files),
                },
            }
        )

    def _resolve_osf(self, link: ArtifactLink) -> ArtifactLink:
        match = _OSF_ID_RE.search(link.url)
        if not match:
            return self._resolve_http_head(link)
        node_id = match.group(1)
        resp = self.client.get(f"https://api.osf.io/v2/nodes/{node_id}/")
        if resp.status_code == 404:
            return link.model_copy(update={"status": ArtifactStatus.BROKEN_404})
        if resp.status_code in {401, 403}:
            return link.model_copy(update={"status": ArtifactStatus.BLOCKED_ACCESS})
        if resp.status_code >= 400:
            return link.model_copy(update={"status": ArtifactStatus.UNRESOLVED})
        attrs = resp.json().get("data", {}).get("attributes", {})
        is_public = attrs.get("public", True)
        status = ArtifactStatus.RESOLVED if is_public else ArtifactStatus.BLOCKED_ACCESS
        return link.model_copy(
            update={
                "status": status,
                "metadata": {**link.metadata, "osf_id": node_id, "public": is_public},
            }
        )

    def _resolve_http_head(self, link: ArtifactLink) -> ArtifactLink:
        if not is_safe_public_url(link.url, resolve_dns=self._owns_client):
            return link.model_copy(
                update={
                    "status": ArtifactStatus.UNRESOLVED,
                    "metadata": {**link.metadata, "blocked_reason": "unsafe_or_private_url"},
                }
            )
        try:
            resp = self.client.head(link.url)
            if resp.status_code == 405:
                resp = self.client.get(link.url)
        except UnsafeUrlError:
            return link.model_copy(
                update={
                    "status": ArtifactStatus.UNRESOLVED,
                    "metadata": {**link.metadata, "blocked_reason": "unsafe_redirect_target"},
                }
            )
        hops = [str(hop.url) for hop in (*resp.history, resp)]
        if 300 <= resp.status_code < 400 and "location" in resp.headers:
            hops.append(urljoin(str(resp.url), resp.headers["location"]))
        if any(not is_safe_public_url(hop, resolve_dns=self._owns_client) for hop in hops):
            return link.model_copy(
                update={
                    "status": ArtifactStatus.UNRESOLVED,
                    "metadata": {**link.metadata, "blocked_reason": "unsafe_redirect_target"},
                }
            )
        if resp.status_code == 404:
            status = ArtifactStatus.BROKEN_404
        elif resp.status_code in {401, 403}:
            status = ArtifactStatus.BLOCKED_ACCESS
        elif 200 <= resp.status_code < 400:
            status = ArtifactStatus.RESOLVED
        else:
            status = ArtifactStatus.UNRESOLVED
        return link.model_copy(update={"status": status})

    def resolve_link(
        self,
        link: ArtifactLink,
        *,
        workdir: Path | None = None,
    ) -> tuple[ArtifactLink, list[str], list[str], list[str], list[str]]:
        """Resolve a single ArtifactLink and inspect local repo files when cloned."""
        if not is_safe_public_url(link.url, resolve_dns=self._owns_client):
            return (
                link.model_copy(
                    update={
                        "status": ArtifactStatus.UNRESOLVED,
                        "metadata": {**link.metadata, "blocked_reason": "unsafe_or_private_url"},
                    }
                ),
                [],
                [],
                [],
                [],
            )
        if link.host == "github":
            return self._resolve_github(link, workdir)
        if link.host == "zenodo":
            return self._resolve_zenodo(link), [], [], [], []
        if link.host == "osf":
            return self._resolve_osf(link), [], [], [], []
        return self._resolve_http_head(link), [], [], [], []

    def resolve_manifest(
        self,
        manifest: ArtifactManifest,
        *,
        workdir: Path | None = None,
    ) -> ArtifactManifest:
        """Resolve all links in an ArtifactManifest and merge discovered files."""
        resolved_links: list[ArtifactLink] = []
        code_set = set(manifest.code_files)
        data_set = set(manifest.data_files)
        manifest_set = set(manifest.manifest_files)
        readme_set = set(manifest.readme_files)

        for link in manifest.links:
            updated, code_f, data_f, man_f, readme_f = self.resolve_link(link, workdir=workdir)
            resolved_links.append(updated)
            code_set.update(code_f)
            data_set.update(data_f)
            manifest_set.update(man_f)
            readme_set.update(readme_f)

        return ArtifactManifest(
            links=resolved_links,
            code_files=sorted(code_set),
            data_files=sorted(data_set),
            manifest_files=sorted(manifest_set),
            readme_files=sorted(readme_set),
        )

    def resolve_bundle(self, bundle: PaperBundle, *, workdir: Path | None = None) -> PaperBundle:
        """Return a copy of PaperBundle with its ArtifactManifest resolved."""
        resolved = self.resolve_manifest(bundle.artifacts, workdir=workdir)
        return bundle.model_copy(update={"artifacts": resolved})
