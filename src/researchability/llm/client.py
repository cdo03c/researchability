"""Gemini API wrapper with structured Pydantic outputs, model tiering, and SQLite caching."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel, Field

T = TypeVar("T", bound=BaseModel)


class MissingApiKeyError(RuntimeError):
    """Raised when a live Gemini API call is requested without GEMINI_API_KEY."""


class GeminiApiError(RuntimeError):
    """Raised when the Gemini API request or response validation fails."""


class ModelTier(str, Enum):
    """Model tiers separating fast extraction/triage from deeper verification."""

    FAST = "fast"
    REASONING = "reasoning"


@dataclass(frozen=True)
class GeminiConfig:
    """Configuration for Gemini API calls and local response caching."""

    api_key: str | None = None
    fast_model: str = "gemini-2.5-flash"
    reasoning_model: str = "gemini-2.5-pro"
    temperature: float = 0.0
    cache_path: Path = Path(".researchability_cache/llm_cache.sqlite")
    min_request_interval_sec: float = 0.0

    @classmethod
    def from_env(cls, load_dotenv_file: bool = True) -> GeminiConfig:
        """Build configuration from environment variables."""
        if load_dotenv_file:
            load_dotenv()
        cache_dir = Path(os.getenv("RESEARCHABILITY_CACHE_DIR", ".researchability_cache"))
        return cls(
            api_key=os.getenv("GEMINI_API_KEY"),
            fast_model=os.getenv("RESEARCHABILITY_FAST_MODEL", "gemini-2.5-flash"),
            reasoning_model=os.getenv("RESEARCHABILITY_REASONING_MODEL", "gemini-2.5-pro"),
            temperature=0.0,
            cache_path=cache_dir / "llm_cache.sqlite",
            min_request_interval_sec=float(os.getenv("RESEARCHABILITY_MIN_INTERVAL_SEC", "0.0")),
        )

    def model_for_tier(self, tier: ModelTier) -> str:
        """Return the pinned model identifier for a tier."""
        return self.fast_model if tier == ModelTier.FAST else self.reasoning_model


class CacheMetadata(BaseModel):
    """Metadata attached to a cached structured LLM response."""

    cache_key: str
    paper_id: str
    gate_id: str
    prompt_version: str
    model_id: str
    from_cache: bool = False


class CachedStructuredResult(BaseModel):
    """Envelope returned alongside validated Pydantic output."""

    payload_json: str
    metadata: CacheMetadata


class ResponseCache:
    """Deterministic SQLite cache keyed by paper, gate, prompt version, model, and input hash."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_responses (
                    cache_key TEXT PRIMARY KEY,
                    paper_id TEXT NOT NULL,
                    gate_id TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    @staticmethod
    def make_key(
        paper_id: str,
        gate_id: str,
        prompt_version: str,
        model_id: str,
        prompt_text: str,
        system_instruction: str = "",
    ) -> tuple[str, str]:
        """Compute deterministic (cache_key, input_hash) tuple."""
        input_hash = hashlib.sha256(f"{system_instruction}\n---\n{prompt_text}".encode("utf-8")).hexdigest()
        composite = "|".join([paper_id, gate_id, prompt_version, model_id, input_hash])
        cache_key = hashlib.sha256(composite.encode("utf-8")).hexdigest()
        return cache_key, input_hash

    def get(self, cache_key: str) -> str | None:
        """Fetch cached response JSON by key, or None on miss."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT response_json FROM llm_responses WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        return row[0] if row else None

    def put(
        self,
        *,
        cache_key: str,
        paper_id: str,
        gate_id: str,
        prompt_version: str,
        model_id: str,
        input_hash: str,
        response_json: str,
    ) -> None:
        """Persist validated response JSON in the cache."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO llm_responses (
                    cache_key, paper_id, gate_id, prompt_version,
                    model_id, input_hash, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    paper_id,
                    gate_id,
                    prompt_version,
                    model_id,
                    input_hash,
                    response_json,
                    time.time(),
                ),
            )


TransportCallable = Callable[[str, str, str, type[BaseModel], float], str]


class GeminiClient:
    """Structured-output Gemini client with deterministic caching and rate limiting."""

    def __init__(
        self,
        config: GeminiConfig | None = None,
        transport: TransportCallable | None = None,
    ) -> None:
        self.config = config or GeminiConfig.from_env()
        self.cache = ResponseCache(self.config.cache_path)
        self._transport = transport
        self._sdk_client = None
        self._last_request_ts = 0.0

    def _get_sdk_client(self):
        if self._sdk_client is not None:
            return self._sdk_client
        if not self.config.api_key or self.config.api_key == "your_gemini_api_key_here":
            raise MissingApiKeyError(
                "GEMINI_API_KEY is not set. Export GEMINI_API_KEY or add it to .env."
            )
        from google import genai

        self._sdk_client = genai.Client(api_key=self.config.api_key)
        return self._sdk_client

    def _default_transport(
        self,
        model_id: str,
        prompt: str,
        system_instruction: str,
        schema_cls: type[BaseModel],
        temperature: float,
    ) -> str:
        from google.genai import types

        client = self._get_sdk_client()
        config = types.GenerateContentConfig(
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema_cls,
            system_instruction=system_instruction or None,
        )
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=prompt,
                config=config,
            )
        except Exception as exc:  # pragma: no cover - live API errors
            raise GeminiApiError(f"Gemini API call failed for model {model_id}: {exc}") from exc

        text = getattr(response, "text", None)
        if not text:
            raise GeminiApiError(f"Gemini returned an empty response for model {model_id}")
        return text

    def _respect_rate_limit(self) -> None:
        if self.config.min_request_interval_sec <= 0:
            return
        elapsed = time.monotonic() - self._last_request_ts
        remaining = self.config.min_request_interval_sec - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_ts = time.monotonic()

    def generate_structured(
        self,
        *,
        paper_id: str,
        gate_id: str,
        prompt: str,
        schema_cls: type[T],
        prompt_version: str = "v1",
        tier: ModelTier = ModelTier.FAST,
        system_instruction: str = "",
        use_cache: bool = True,
    ) -> tuple[T, CacheMetadata]:
        """Return a validated Pydantic instance and provenance metadata."""
        model_id = self.config.model_for_tier(tier)
        cache_key, input_hash = self.cache.make_key(
            paper_id=paper_id,
            gate_id=gate_id,
            prompt_version=prompt_version,
            model_id=model_id,
            prompt_text=prompt,
            system_instruction=system_instruction,
        )

        if use_cache:
            cached_json = self.cache.get(cache_key)
            if cached_json is not None:
                parsed = schema_cls.model_validate_json(cached_json)
                return parsed, CacheMetadata(
                    cache_key=cache_key,
                    paper_id=paper_id,
                    gate_id=gate_id,
                    prompt_version=prompt_version,
                    model_id=model_id,
                    from_cache=True,
                )

        self._respect_rate_limit()
        caller = self._transport or self._default_transport
        raw_json = caller(
            model_id,
            prompt,
            system_instruction,
            schema_cls,
            self.config.temperature,
        )

        try:
            parsed = schema_cls.model_validate_json(raw_json)
        except Exception as exc:
            raise GeminiApiError(
                f"Response for {gate_id} did not match schema {schema_cls.__name__}: {exc}"
            ) from exc

        normalized_json = parsed.model_dump_json()
        if use_cache:
            self.cache.put(
                cache_key=cache_key,
                paper_id=paper_id,
                gate_id=gate_id,
                prompt_version=prompt_version,
                model_id=model_id,
                input_hash=input_hash,
                response_json=normalized_json,
            )

        return parsed, CacheMetadata(
            cache_key=cache_key,
            paper_id=paper_id,
            gate_id=gate_id,
            prompt_version=prompt_version,
            model_id=model_id,
            from_cache=False,
        )
