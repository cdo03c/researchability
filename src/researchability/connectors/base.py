"""Shared HTTP client lifecycle and context-manager base for repository connectors."""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Self

import httpx

logger = logging.getLogger(__name__)
_TRANSIENT_STATUS_CODES = {429, 502, 503, 504}


class RetryTransport(httpx.BaseTransport):
    """HTTP transport wrapper adding bounded retries with exponential backoff."""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        *,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
    ) -> None:
        self.inner = inner
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds

    def handle_request(self, request: httpx.Request) -> httpx.Response:
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
            transport = RetryTransport(
                httpx.HTTPTransport(),
                max_retries=max_retries,
                backoff_seconds=retry_backoff,
            )
            self.client = httpx.Client(
                transport=transport,
                timeout=timeout,
                follow_redirects=True,
            )

    def close(self) -> None:
        """Close the HTTP client if owned by this connector."""
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
