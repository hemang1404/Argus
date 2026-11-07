"""Universal OpenAI-compatible provider adapter with secret scrubbing and timeout handling."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from argus.providers.models import ProviderConfig, PROVIDER_PRESETS
from argus.providers.storage import scrub_secrets


class ProviderError(Exception):
    """Exception raised by provider operations with scrubbed credentials."""
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
        is_retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after
        self.is_retryable = is_retryable


@dataclass
class CompletionResponse:
    """Unified response object returned by model inference dispatches."""
    content: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: float
    model: str
    raw_response: Dict[str, Any] = field(default_factory=dict)
    cache_read_tokens: int = 0
    thinking_tokens: int = 0
    net_prompt_tokens: Optional[int] = None
    transmitted_payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectionTestResult:
    """Outcome of testing connectivity to a configured provider."""
    success: bool
    status_code: Optional[int]
    latency_ms: float
    model: str
    message: str
    response_sample: str = ""


class ProviderAdapter(ABC):
    """Abstract interface for LLM provider adapters."""

    @abstractmethod
    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        effort: Optional[str] = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        """Execute a chat completion request."""
        raise NotImplementedError

    @abstractmethod
    def test_connection(self) -> ConnectionTestResult:
        """Test API reachability and authentication."""
        raise NotImplementedError


class OpenAICompatibleAdapter(ProviderAdapter):
    """Universal adapter for any OpenAI-compatible API endpoint.
    
    Compatible with:
    - OpenAI official API
    - OpenRouter
    - DeepSeek
    - Ollama / LM Studio (local endpoints)
    - vLLM / LocalAI / custom endpoints
    """

    def __init__(self, config: ProviderConfig, client: Optional[httpx.Client] = None) -> None:
        self.config = config
        self._client = client
        self._managed_client: Optional[httpx.Client] = None

    def _get_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        if self._managed_client is None or self._managed_client.is_closed:
            self._managed_client = httpx.Client(
                timeout=self.config.timeout_seconds,
                transport=httpx.HTTPTransport(retries=0),
            )
        return self._managed_client

    def close(self) -> None:
        """Close managed HTTP client resources deterministically."""
        if self._managed_client is not None:
            if not self._managed_client.is_closed:
                try:
                    self._managed_client.close()
                except Exception:
                    pass
            self._managed_client = None

    def __enter__(self) -> OpenAICompatibleAdapter:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _get_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Argus/1.0",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if self.config.extra_headers:
            headers.update(self.config.extra_headers)
        return headers

    def _scrub(self, text: str) -> str:
        keys = [self.config.api_key] if self.config.api_key else []
        return scrub_secrets(text, keys)

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        effort: Optional[str] = None,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        """Execute chat completion dispatch against the configured endpoint."""
        preset = PROVIDER_PRESETS.get(self.config.name.lower())
        if preset and preset.requires_key and not self.config.api_key:
            raise ProviderError(
                f"Missing API key for provider '{self.config.name}'. "
                f"Please set your API key using 'argus provider set --name {self.config.name} --api-key <KEY>' "
                f"or configure the appropriate environment variable."
            )

        active_model = model or kwargs.get("model") or self.config.model
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = self._get_headers()
        eff = effort or kwargs.get("reasoning_effort") or getattr(self.config, "reasoning_effort", None)
        max_comp_tokens = kwargs.get("max_completion_tokens") or getattr(self.config, "max_completion_tokens", None)
        token_limit = max_tokens if max_tokens is not None else self.config.max_tokens

        # Scrub all outgoing message contents to prevent accidental secret leakage
        safe_messages = [
            {"role": m.get("role", "user"), "content": scrub_secrets(m.get("content", ""))}
            for m in messages
        ]

        payload: Dict[str, Any] = {
            "model": active_model,
            "messages": safe_messages,
            "temperature": self.config.temperature if temperature is None else temperature,
        }
        if max_comp_tokens is not None:
            payload["max_completion_tokens"] = max_comp_tokens
        elif token_limit is not None:
            payload["max_tokens"] = token_limit

        if eff is not None:
            payload["reasoning_effort"] = eff

        start_time = time.perf_counter()
        client = self._get_client()

        try:
            resp = client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as ex:
            dur = (time.perf_counter() - start_time) * 1000
            err_msg = self._scrub(f"Request to {self.config.name} timed out after {self.config.timeout_seconds}s: {ex}")
            raise ProviderError(err_msg, status_code=504) from None
        except httpx.RequestError as ex:
            dur = (time.perf_counter() - start_time) * 1000
            err_msg = self._scrub(f"Network error connecting to {self.config.name} ({url}): {ex}")
            raise ProviderError(err_msg) from None

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        if resp.status_code != 200:
            error_body = self._scrub(resp.text[:300])
            if resp.status_code == 401:
                msg = f"Authentication failed (HTTP 401) for provider '{self.config.name}'. Please verify your API key."
            elif resp.status_code == 403:
                msg = f"Access forbidden (HTTP 403) for provider '{self.config.name}'. Check permissions or account balance."
            elif resp.status_code == 404:
                msg = f"Endpoint not found (HTTP 404) at '{url}'. Verify base_url and model identifier '{self.config.model}'."
            elif resp.status_code == 429:
                msg = f"Rate limit reached (HTTP 429) for provider '{self.config.name}': {error_body}"
                retry_after_hdr = resp.headers.get("retry-after")
                retry_after_sec = None
                if retry_after_hdr:
                    try:
                        retry_after_sec = float(retry_after_hdr)
                    except ValueError:
                        pass
                raise ProviderError(msg, status_code=429, retry_after=retry_after_sec, is_retryable=True)
            else:
                msg = f"Provider '{self.config.name}' returned HTTP {resp.status_code}: {error_body}"
            raise ProviderError(msg, status_code=resp.status_code)

        try:
            data = resp.json()
        except Exception as ex:
            raise ProviderError(f"Failed to parse JSON response from {self.config.name}: {ex}") from None

        choices = data.get("choices", [])
        if not choices:
            raise ProviderError(f"Provider {self.config.name} returned an empty choices array.")

        choice = choices[0]
        message = choice.get("message", {})
        content = message.get("content", "") or ""

        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0) or 0
        completion_tokens = usage.get("completion_tokens", 0) or 0
        prompt_details = usage.get("prompt_tokens_details") or {}
        cache_read_tokens = (
            prompt_details.get("cached_tokens", 0)
            or usage.get("prompt_cache_hit_tokens", 0)
            or usage.get("cached_tokens", 0)
            or 0
        )

        # Fallback estimation if provider returns 0 tokens
        if prompt_tokens == 0 and completion_tokens == 0:
            prompt_tokens = sum(len(m.get("content", "").split()) for m in messages)
            completion_tokens = len(content.split())

        logged_payload = {k: v for k, v in payload.items() if k != "messages"}
        logged_payload["messages_count"] = len(messages)

        return CompletionResponse(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=duration_ms,
            model=data.get("model", self.config.model),
            raw_response=data,
            cache_read_tokens=cache_read_tokens,
            transmitted_payload=logged_payload,
        )

    def test_connection(self) -> ConnectionTestResult:
        """Test API connectivity by sending a lightweight ping request."""
        messages = [{"role": "user", "content": "ping"}]
        start_time = time.perf_counter()

        try:
            resp = self.chat_completion(messages=messages, max_tokens=10)
            latency = resp.duration_ms
            sample = resp.content.strip().replace("\n", " ")[:60]
            return ConnectionTestResult(
                success=True,
                status_code=200,
                latency_ms=latency,
                model=resp.model,
                message=f"Successfully connected to '{self.config.name}'. Model '{resp.model}' responded.",
                response_sample=sample,
            )
        except ProviderError as ex:
            latency = round((time.perf_counter() - start_time) * 1000, 2)
            return ConnectionTestResult(
                success=False,
                status_code=ex.status_code,
                latency_ms=latency,
                model=self.config.model,
                message=str(ex),
            )
        except Exception as ex:
            latency = round((time.perf_counter() - start_time) * 1000, 2)
            return ConnectionTestResult(
                success=False,
                status_code=None,
                latency_ms=latency,
                model=self.config.model,
                message=self._scrub(f"Unexpected connection error: {ex}"),
            )
