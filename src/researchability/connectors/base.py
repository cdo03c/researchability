"""Shared HTTP client lifecycle and context-manager base for repository connectors."""

from __future__ import annotations

import contextlib
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any, Self
from urllib.parse import urljoin

import httpx

from researchability.connectors.links import is_safe_public_url, safe_resolve_under

logger = logging.getLogger(__name__)
_TRANSIENT_STATUS_CODES = {429, 502, 503, 504}


class UnsafeUrlError(httpx.RequestError):
    """Raised when an HTTP request or redirect targets a non-public/unsafe URL."""


def _validate_public_request_url(request: httpx.Request) -> None:
    if not is_safe_public_url(str(request.url), resolve_dns=True):
        raise UnsafeUrlError(f"Blocked unsafe or non-public URL: {request.url}", request=request)


class RetryTransport(httpx.BaseTransport):
    """HTTP transport wrapper adding bounded retries with exponential backoff."""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        *,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        enforce_public_urls: bool = False,
        resolve_dns: bool = False,
    ) -> None:
        self.inner = inner
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.enforce_public_urls = enforce_public_urls
        self.resolve_dns = resolve_dns

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.enforce_public_urls and not is_safe_public_url(
            str(request.url), resolve_dns=self.resolve_dns
        ):
            raise UnsafeUrlError(
                f"Blocked unsafe or non-public URL: {request.url}", request=request
            )
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.inner.handle_request(request)
                if (
                    response.status_code not in _TRANSIENT_STATUS_CODES
                    or attempt >= self.max_retries
                ):
                    return response
                retry_after = response.headers.get("Retry-After")
                response.close()
                delay = self.backoff_seconds * (2**attempt)
                if retry_after:
                    with contextlib.suppress(ValueError):
                        delay = min(max(float(retry_after), 0.0), 10.0)
                if delay > 0:
                    logger.warning(
                        "HTTP %s for %s; retrying attempt %s/%s in %.2fs",
                        response.status_code,
                        request.url,
                        attempt + 1,
                        self.max_retries,
                        delay,
                    )
                    time.sleep(delay)
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise
                delay = self.backoff_seconds * (2**attempt)
                logger.warning(
                    "HTTP transport error for %s (%s); retrying attempt %s/%s",
                    request.url,
                    exc,
                    attempt + 1,
                    self.max_retries,
                )
                if delay > 0:
                    time.sleep(delay)
        if last_exc is not None:  # pragma: no cover
            raise last_exc
        raise RuntimeError("Unreachable retry state")  # pragma: no cover

    def close(self) -> None:
        self.inner.close()


def create_safe_http_client(
    *,
    timeout: float = 30.0,
    max_retries: int = 2,
    retry_backoff: float = 0.5,
) -> httpx.Client:
    """Create an httpx.Client with retry backoff and per-hop SSRF validation."""
    transport = RetryTransport(
        httpx.HTTPTransport(),
        max_retries=max_retries,
        backoff_seconds=retry_backoff,
        enforce_public_urls=True,
        resolve_dns=True,
    )
    return httpx.Client(
        transport=transport,
        timeout=timeout,
        follow_redirects=True,
        event_hooks={"request": [_validate_public_request_url]},
    )


class HttpConnector:
    """Base class managing an optional caller-injected or owned httpx.Client."""

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
        *,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
    ) -> None:
        self._owns_client = http_client is None
        if http_client is not None:
            self.client = http_client
        else:
            self.client = create_safe_http_client(
                timeout=timeout,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
            )

    def close(self) -> None:
        """Close the HTTP client if owned by this connector."""
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def stream_download_file(
    client: httpx.Client,
    url: str,
    dest: Path,
    *,
    max_bytes: int,
    already_downloaded: int = 0,
    resolve_dns: bool = False,
) -> tuple[int, bool, int | None]:
    """Stream-download a public HTTP(S) URL to ``dest`` enforcing byte and SSRF limits.

    Returns ``(bytes_written, exceeded_budget, status_code)``. Deletes partial files on
    budget overflow or non-200 status. Raises ``UnsafeUrlError`` if URL or redirect targets
    resolve to a private/internal address.
    """
    if not is_safe_public_url(url, resolve_dns=resolve_dns):
        raise UnsafeUrlError(f"Blocked unsafe or non-public URL: {url}")

    written = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with client.stream("GET", url) as resp:
        hops = [str(hop.url) for hop in (*resp.history, resp)]
        if 300 <= resp.status_code < 400 and "location" in resp.headers:
            hops.append(urljoin(str(resp.url), resp.headers["location"]))
        if any(not is_safe_public_url(hop, resolve_dns=resolve_dns) for hop in hops):
            raise UnsafeUrlError(f"Blocked unsafe redirect target while fetching: {url}")

        if resp.status_code != 200:
            return 0, False, resp.status_code

        content_len = resp.headers.get("Content-Length")
        if (
            content_len
            and content_len.isdigit()
            and already_downloaded + int(content_len) > max_bytes
        ):
            return 0, True, resp.status_code

        exceeded = False
        with dest.open("wb") as out_fh:
            for chunk in resp.iter_bytes(chunk_size=65_536):
                written += len(chunk)
                if already_downloaded + written > max_bytes:
                    exceeded = True
                    break
                out_fh.write(chunk)

        if exceeded:
            dest.unlink(missing_ok=True)
            return 0, True, resp.status_code
        return written, False, resp.status_code


def download_osf_storage_files(
    client: httpx.Client,
    listing_url: str,
    dest_dir: Path,
    *,
    max_bytes: int,
    download_files: bool = True,
    resolve_dns: bool = False,
    max_pages: int = 10,
) -> tuple[list[str], int, bool]:
    """List and optionally download files from an OSF v2 osfstorage tree into ``dest_dir``.

    Returns ``(discovered_rel_paths, total_bytes_reported, budget_exceeded)``.
    """
    discovered: list[str] = []
    total_reported = 0
    downloaded_bytes = 0
    queue: deque[str] = deque([listing_url])
    seen_urls: set[str] = set()
    pages = 0

    while queue and pages < max_pages:
        url = queue.popleft()
        if url in seen_urls or not is_safe_public_url(url, resolve_dns=resolve_dns):
            continue
        seen_urls.add(url)
        pages += 1

        try:
            resp = client.get(url)
        except httpx.HTTPError:
            continue
        if resp.status_code != 200:
            continue
        try:
            payload = resp.json()
        except ValueError:
            continue

        items: list[dict[str, Any]] = payload.get("data") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            attrs = item.get("attributes") or {}
            kind = str(attrs.get("kind") or "")
            if kind == "folder":
                rel_folder = (
                    item.get("relationships", {})
                    .get("files", {})
                    .get("links", {})
                    .get("related", {})
                )
                href = rel_folder.get("href") if isinstance(rel_folder, dict) else None
                if isinstance(href, str) and href:
                    queue.append(href)
                continue
            if kind != "file":
                continue

            raw_rel = str(attrs.get("materialized_path") or attrs.get("name") or "").lstrip("/")
            if not raw_rel:
                continue
            file_size = int(attrs.get("size") or 0)
            total_reported += max(0, file_size)
            if total_reported > max_bytes:
                return discovered, total_reported, True

            discovered.append(raw_rel)
            dl_url = (item.get("links") or {}).get("download")
            if not download_files or not isinstance(dl_url, str) or not dl_url:
                continue

            target = safe_resolve_under(dest_dir, raw_rel)
            if target is None:
                continue
            try:
                written, exceeded, _code = stream_download_file(
                    client,
                    dl_url,
                    target,
                    max_bytes=max_bytes,
                    already_downloaded=downloaded_bytes,
                    resolve_dns=resolve_dns,
                )
            except UnsafeUrlError:
                continue
            if exceeded:
                return discovered, max(total_reported, downloaded_bytes), True
            downloaded_bytes += written

        next_url = (payload.get("links") or {}).get("next")
        if isinstance(next_url, str) and next_url:
            queue.append(next_url)

    return discovered, max(total_reported, downloaded_bytes), False
