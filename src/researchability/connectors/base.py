"""Shared HTTP client lifecycle and context-manager base for repository connectors."""

from __future__ import annotations

from typing import Self

import httpx


class HttpConnector:
    """Base class managing an optional caller-injected or owned httpx.Client."""

    def __init__(self, http_client: httpx.Client | None = None, timeout: float = 30.0) -> None:
        self._owns_client = http_client is None
        self.client = http_client or httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        """Close the HTTP client if owned by this connector."""
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
