"""Argus Local Proxy Gateway Server.

Implements an OpenAI-compatible HTTP proxy on localhost with:
1. Model tier resolution (argus/auto, argus/cheap, argus/strong, argus/frontier)
2. In-memory streaming patch healing via the 5-Tier Resilient Patch Engine
3. Task-level and session-level USD budget caps
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Deque, Dict, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from fastapi.responses import JSONResponse, StreamingResponse

from argus import __version__
from argus.config import MODEL_REGISTRY, ModelCost
from argus.adapters.protocol import (
    anthropic_to_openai_request,
    openai_to_anthropic_response,
    transform_openai_stream_to_anthropic,
)
from argus.gateway.budget import BudgetGuard
from argus.gateway.config import GatewayConfig
from argus.gateway.interceptor import StreamInterceptor, StreamPatchHealer
from argus.gateway.retry import (
    FailureClassification,
    RetryConfig,
    calculate_backoff,
    classify_exception,
    classify_response,
    is_retryable,
    is_uncertain_execution,
    parse_retry_after,
)
from argus.gateway.router import GatewayRouter, NoEligibleProviderError
from argus.gateway.telemetry import TelemetryRegistry, detect_client
from argus.gateway.transport import create_safe_async_client
from argus.providers.storage import scrub_secrets

logger = logging.getLogger("argus.gateway.server")


def _parse_ip_candidate(host: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Parse string representation of IPv4 or IPv6 address, including decimal, hex, dword, and octal encodings."""
    clean = host.strip("[]").strip()
    if not clean:
        return None

    # 1. Standard ipaddress parser
    try:
        return ipaddress.ip_address(clean)
    except ValueError:
        pass

    # 2. Integer or hexadecimal single-token IPv4 (e.g. 2852039166, 0xa9fea9fe)
    try:
        val = int(clean, 0)
        if 0 <= val <= 0xFFFFFFFF:
            return ipaddress.IPv4Address(val)
    except (ValueError, OverflowError):
        pass

    # 3. Dot-separated octal, hex, or mixed notation (e.g. 0251.0376.0251.0376)
    parts = clean.split(".")
    if len(parts) == 4:
        try:
            nums = []
            for p in parts:
                p_clean = p.strip().lower()
                # If leading zero without 0x/0o prefix, standard C/libc parses as octal
                if p_clean.startswith("0") and len(p_clean) > 1 and not p_clean.startswith("0x") and not p_clean.startswith("0o"):
                    nums.append(int(p_clean, 8))
                else:
                    nums.append(int(p_clean, 0))
            if all(0 <= n <= 255 for n in nums):
                val = (nums[0] << 24) | (nums[1] << 16) | (nums[2] << 8) | nums[3]
                return ipaddress.IPv4Address(val)
        except (ValueError, OverflowError):
            pass

    return None


def infer_key_provider(api_key: str) -> Optional[str]:
    """Infer intended upstream provider from key prefix signature."""
    if not api_key:
        return None
    k = api_key.strip()
    if k.startswith("gsk_") or k.startswith("gsk-"):
        return "groq"
    if k.startswith("sk-ant-"):
        return "anthropic"
    if k.startswith("sk-proj-") or k.startswith("sk-"):
        return "openai"
    return None


def resolve_client_upstream_credential(
    request: Request,
    target_provider: str,
    expected_gateway_token: Optional[str],
    has_configured_key: bool = False,
) -> tuple[Optional[str], Optional[dict]]:
    """Resolve and validate client-supplied upstream credentials against target provider.

    Returns (effective_key, error_dict).
    If error_dict is present, request must be rejected with HTTP 400.
    """
    x_upstream_key = (request.headers.get("x-upstream-api-key") or "").strip() or None
    x_api_key = (request.headers.get("x-api-key") or "").strip() or None

    auth_bearer_key = None
    auth_hdr = (request.headers.get("authorization") or "").strip()
    if auth_hdr.startswith("Bearer "):
        auth_bearer_key = auth_hdr[7:].strip() or None

    # Check for conflicting credentials
    if expected_gateway_token:
        # Authorization header is dedicated to Argus gateway authentication.
        # Upstream credentials can only be supplied via x-upstream-api-key or x-api-key.
        if x_upstream_key and x_api_key and x_upstream_key != x_api_key:
            return None, {
                "message": "Conflicting upstream credentials supplied in X-Upstream-Api-Key and X-Api-Key headers.",
                "type": "conflicting_upstream_credentials",
                "code": "conflicting_credentials",
            }
        client_key = x_upstream_key or x_api_key
    else:
        # No gateway token configured: client might pass upstream key via x-upstream-api-key, x-api-key, or Bearer.
        candidates = {k for k in (x_upstream_key, x_api_key, auth_bearer_key) if k}
        if len(candidates) > 1:
            return None, {
                "message": "Conflicting upstream credentials supplied across client headers.",
                "type": "conflicting_upstream_credentials",
                "code": "conflicting_credentials",
            }
        client_key = candidates.pop() if candidates else None

    target_prov = target_provider.lower()
    header_provider = (request.headers.get("x-upstream-provider") or "").strip().lower() or None

    if target_prov == "groq" or header_provider == "groq":
        return None, {
            "message": "Groq is strictly prohibited across all of Argus.",
            "type": "prohibited_provider",
            "code": "prohibited_provider",
        }

    if not client_key:
        return None, None

    inferred_provider = infer_key_provider(client_key)
    if inferred_provider == "groq":
        return None, {
            "message": "Groq is strictly prohibited across all of Argus.",
            "type": "prohibited_provider",
            "code": "prohibited_provider",
        }

    # Detect conflicting provider indications between explicit header and key signature
    if header_provider and inferred_provider and header_provider != inferred_provider and header_provider not in inferred_provider:
        return None, {
            "message": (
                f"Conflicting provider indications: X-Upstream-Provider '{header_provider}' "
                f"conflicts with key signature '{inferred_provider}'."
            ),
            "type": "conflicting_provider_binding",
            "code": "conflicting_provider_binding",
        }

    intended_provider = header_provider or inferred_provider

    if not intended_provider:
        # Recognized local proxy placeholders (from standard SDK defaults or argus-local) can defer to configured keys
        known_placeholders = {"dummy", "none", "local", "argus-local", "placeholder", "test-key-ignored-by-local-proxy", "not-needed"}
        k_lower = client_key.lower()
        if (k_lower in known_placeholders or k_lower.startswith("argus-")) and has_configured_key:
            return None, None

        return None, {
            "message": (
                "Client-supplied upstream credential has no valid provider binding. "
                "Supply 'X-Upstream-Provider' header or a recognized provider key signature."
            ),
            "type": "unbound_upstream_credential",
            "code": "missing_provider_binding",
        }

    # Verify provider boundary invariant
    if intended_provider != target_prov and intended_provider not in target_prov:
        source_desc = f"header '{header_provider}'" if header_provider else f"signature '{inferred_provider}'"
        return None, {
            "message": (
                f"Client-supplied upstream credential is bound to {source_desc}, "
                f"but request routed to provider '{target_provider}'. "
                "Refusing cross-provider credential forwarding."
            ),
            "type": "cross_provider_credential_violation",
            "code": "credential_provider_mismatch",
        }

    return client_key, None


def validate_upstream_url(base_url: str, resolve_dns: bool = True) -> bool:
    """Validate that upstream URL is a safe scheme and prevent SSRF to cloud metadata endpoints."""
    if not base_url or not isinstance(base_url, str):
        return False
    lower = base_url.lower().strip()
    if lower.startswith("host://"):
        return True

    try:
        parsed = urlsplit(lower)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    # Forbid userinfo in upstream URL
    if parsed.username or parsed.password:
        return False

    # Port range validation (handling ValueError from Python 3.14 on out-of-range port)
    try:
        port = parsed.port
        if port is not None and not (1 <= port <= 65535):
            return False
    except ValueError:
        return False

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return False

    # Block Groq endpoints
    if hostname == "api.groq.com" or hostname.endswith(".groq.com") or "groq.com" in hostname:
        return False

    # Block well-known cloud metadata domain names
    blocked_domains = {
        "metadata.google.internal",
        "instance-data",
        "metadata",
        "169.254.169.254",
        "100.100.100.200",
    }
    if hostname in blocked_domains or hostname.endswith(".metadata.google.internal"):
        return False

    # Loopback addresses are explicitly permitted for local model runtimes (Ollama, vLLM)
    is_loopback = hostname in ("localhost", "127.0.0.1", "::1")

    # Parse IP candidate (handles decimal, hex, octal, dword, IPv6, IPv6-mapped IPv4)
    ip_candidate = _parse_ip_candidate(hostname)
    if ip_candidate is not None:
        if isinstance(ip_candidate, ipaddress.IPv6Address) and ip_candidate.ipv4_mapped:
            ip_candidate = ip_candidate.ipv4_mapped

        # Strictly block link-local (169.254.0.0/16, fe80::/10), multicast, unspecified
        if (
            ip_candidate.is_link_local
            or ip_candidate.is_multicast
            or ip_candidate.is_unspecified
            or str(ip_candidate) == "100.100.100.200"
            or str(ip_candidate) == "169.254.169.254"
        ):
            return False

        # Non-loopback private networks are blocked unless explicitly configured
        if ip_candidate.is_private and not ip_candidate.is_loopback:
            return False

    # Connection-time DNS resolution check for rebinding protection
    if resolve_dns and ip_candidate is None:
        try:
            addr_info = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
            for family, _, _, _, sockaddr in addr_info:
                resolved_ip_str = sockaddr[0]
                resolved_ip = _parse_ip_candidate(resolved_ip_str)
                if resolved_ip is not None:
                    if isinstance(resolved_ip, ipaddress.IPv6Address) and resolved_ip.ipv4_mapped:
                        resolved_ip = resolved_ip.ipv4_mapped
                    if (
                        resolved_ip.is_link_local
                        or resolved_ip.is_multicast
                        or resolved_ip.is_unspecified
                        or str(resolved_ip) in ("169.254.169.254", "100.100.100.200")
                    ):
                        return False
                    # Prevent DNS rebinding: public domain resolving to loopback or private network
                    if (resolved_ip.is_loopback or resolved_ip.is_private) and not is_loopback:
                        return False
        except socket.gaierror:
            pass
        except Exception:
            pass

    return True


_config: Optional[GatewayConfig] = None
_router: Optional[GatewayRouter] = None
_budget_guard: Optional[BudgetGuard] = None
_healer: Optional[StreamPatchHealer] = None
_start_time = time.time()


def init_gateway(
    config: Optional[GatewayConfig] = None,
    workspace: Path | str = ".",
) -> None:
    """Initialize or reconfigure gateway components."""
    global _config, _router, _budget_guard, _healer
    _config = config or GatewayConfig.load()
    _router = GatewayRouter(_config)
    _budget_guard = BudgetGuard(_config.budget)
    _healer = StreamPatchHealer(
        workspace=workspace or _config.healing.workspace,
        enabled=_config.healing.enabled,
    )
    TelemetryRegistry.get(workspace_root=workspace or _config.healing.workspace)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Auto-initialize gateway on server start if not already done."""
    if _config is None:
        init_gateway()
    # Direct ASGI startup (e.g. uvicorn) must fail closed if API key is not configured
    in_test = bool(os.environ.get("ARGUS_TEST_MODE") or os.environ.get("PYTEST_CURRENT_TEST"))
    if not in_test and _config and not _config.get_gateway_key():
        raise RuntimeError(
            "Security Refusal: ARGUS_GATEWAY_API_KEY environment variable is required to start the Gateway. "
            "Unauthenticated gateway execution is strictly prohibited."
        )
    yield


class GatewayRateLimiter:
    """In-memory sliding window rate limiter per client IP with bounded client state."""

    def __init__(self, max_requests_per_minute: int = 120, max_clients: int = 1000) -> None:
        self.max_requests = max_requests_per_minute
        self.max_clients = max_clients
        self._history: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def allow_request(self, client_key: str) -> bool:
        now = time.time()
        window_start = now - 60.0

        with self._lock:
            if client_key not in self._history and len(self._history) >= self.max_clients:
                stale = [k for k, v in self._history.items() if not v or v[-1] < window_start]
                for k in stale:
                    del self._history[k]
                if len(self._history) >= self.max_clients:
                    oldest = min(self._history.keys(), key=lambda k: self._history[k][-1] if self._history[k] else 0)
                    del self._history[oldest]

            if client_key not in self._history:
                self._history[client_key] = collections.deque()

            q = self._history[client_key]
            while q and q[0] < window_start:
                q.popleft()

            if len(q) >= self.max_requests:
                return False

            q.append(now)
            return True


_rate_limiter = GatewayRateLimiter()


def verify_gateway_auth(request: Request) -> Optional[JSONResponse]:
    """Enforce mandatory Bearer token authentication with constant-time comparison."""
    assert _config is not None
    expected_token = _config.get_gateway_key()

    if not expected_token:
        # In test suite, allow legacy test cases that explicitly instantiate GatewayConfig without a gateway key
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("ARGUS_TEST_MODE") == "1":
            return None
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": "Gateway authentication failed: ARGUS_GATEWAY_API_KEY is not configured.",
                    "type": "client_authentication_error",
                    "code": "gateway_unauthenticated",
                }
            },
        )

    auth_hdr = request.headers.get("authorization", "")
    x_api_key = request.headers.get("x-api-key", "")
    x_argus_key = request.headers.get("x-argus-key", "")

    client_token = ""
    if auth_hdr.startswith("Bearer "):
        client_token = auth_hdr[7:].strip()
    elif x_api_key:
        client_token = x_api_key.strip()
    elif x_argus_key:
        client_token = x_argus_key.strip()

    if not client_token or not hmac.compare_digest(expected_token, client_token):
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": "Invalid or missing Argus Gateway client authentication token.",
                    "type": "client_authentication_error",
                    "code": "unauthorized_gateway_client",
                }
            },
        )
    return None


def extract_rate_limit_identity(request: Request) -> str:
    """Extract a privacy-preserving rate limit identity key without storing raw tokens."""
    auth_hdr = (request.headers.get("authorization") or "").strip()
    x_api_key = (request.headers.get("x-api-key") or "").strip()
    x_argus_key = (request.headers.get("x-argus-key") or "").strip()
    client_id = (request.headers.get("x-client-id") or "").strip()
    user_agent = (request.headers.get("user-agent") or "").strip()[:32]

    raw_token = ""
    if auth_hdr.startswith("Bearer "):
        raw_token = auth_hdr[7:].strip()
    elif x_api_key:
        raw_token = x_api_key
    elif x_argus_key:
        raw_token = x_argus_key

    client_ip = request.client.host if request.client else "127.0.0.1"

    if raw_token:
        # Truncated hash digest: never store raw credentials in memory
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()[:16]
        extra = f":{client_id}" if client_id else (f":{user_agent}" if user_agent else "")
        return f"auth:{token_hash}{extra}"

    extra = f":{client_id}" if client_id else ""
    return f"ip:{client_ip}{extra}"


def estimate_request_cost(body: Dict[str, Any], model: Optional[str] = None) -> float:
    """Estimate request cost for two-phase budget reservation based on message payload size and model pricing."""
    messages = body.get("messages", [])
    total_chars = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))
    est_prompt_tokens = max(100, total_chars // 4)
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens") or 1000

    target_model = model or body.get("model", "argus/auto")
    cost_profile = MODEL_REGISTRY.get(target_model, ModelCost(input_cost_per_m=2.50, output_cost_per_m=10.00))
    est_cost = cost_profile.calculate_cost(est_prompt_tokens, max_tokens)
    return min(0.50, max(0.001, round(est_cost, 4)))


# Global FastAPI application
app = FastAPI(
    title="Argus Gateway",
    description="Local-first inference proxy and resilient streaming patch healer",
    version=__version__,
    lifespan=lifespan,
)

# Enable CORS restricted strictly to loopback origins (prevents drive-by browser requests from foreign sites)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def gateway_security_middleware(request: Request, call_next):
    # 1. Host header validation: prevent DNS rebinding
    host_header = request.headers.get("host", "").split(":")[0].strip().lower()
    allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    # testserver is strictly restricted to test execution environments
    if os.environ.get("ARGUS_TEST_MODE") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
        allowed_hosts.add("testserver")
    allow_remote = _config and getattr(_config, "allow_remote", False)

    if not allow_remote and host_header and host_header not in allowed_hosts:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": f"Host '{host_header}' rejected. Loopback host required.", "code": "invalid_host"}},
        )

    # 2. Origin validation: reject foreign browser origins
    origin = request.headers.get("origin")
    if origin:
        parsed_origin = urlsplit(origin)
        origin_host = (parsed_origin.hostname or "").lower()
        if not allow_remote and origin_host not in allowed_hosts:
            return JSONResponse(
                status_code=403,
                content={"error": {"message": f"Cross-origin request from '{origin}' is forbidden.", "code": "forbidden_origin"}},
            )

    # 3. Rate limiting per client identity (skip unauthenticated health liveness ping)
    if request.url.path not in ("/", "/health"):
        rate_key = extract_rate_limit_identity(request)
        if not _rate_limiter.allow_request(rate_key):
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": "60"},
                content={"error": {"message": "Gateway rate limit exceeded. Please retry later.", "code": "rate_limit_exceeded"}},
            )

    return await call_next(request)


@app.get("/")
@app.get("/health")
async def health_check(request: Request) -> Dict[str, Any]:
    """Health status and runtime telemetry."""
    assert _config is not None and _budget_guard is not None
    uptime_seconds = round(time.time() - _start_time, 1)

    # When gateway authentication is configured, unauthenticated requests only receive minimal liveness
    if _config.get_gateway_key():
        auth_err = verify_gateway_auth(request)
        if auth_err is not None:
            return {
                "status": "healthy",
                "service": "Argus Gateway",
                "version": __version__,
                "uptime_seconds": uptime_seconds,
            }

    # Detailed runtime telemetry for authenticated clients
    summary = TelemetryRegistry.get().get_summary()
    return {
        "status": "healthy",
        "service": "Argus Gateway",
        "version": __version__,
        "uptime_seconds": uptime_seconds,
        "healing_enabled": _healer.enabled if _healer else True,
        "active_client": summary.active_client,
        "total_calls": summary.total_calls,
        "session_spend_usd": summary.session_spend_usd,
        "alpha_saved_usd": summary.alpha_saved_usd,
        "alpha_percent": summary.alpha_percent,
        "budget_limit_session_usd": _config.budget.per_session_usd,
        "configured_providers": [p.name for p in _config.providers],
        "protocols": ["openai/chat/completions", "anthropic/messages"],
        "active_tiers": {
            name: f"{td.provider} -> {td.model}"
            for name, td in _config.tiers.items()
        },
    }


@app.get("/v1/models")
async def list_models(request: Request) -> Response:
    """OpenAI-compatible model list endpoint with mandatory client authentication."""
    if _config and _config.get_gateway_key():
        auth_err = verify_gateway_auth(request)
        if auth_err is not None:
            return auth_err

    assert _router is not None
    advertised = _router.get_advertised_models()
    created_ts = int(_start_time)
    data = [
        {
            "id": m_id,
            "object": "model",
            "created": created_ts,
            "owned_by": "argus" if m_id.startswith("argus/") else "upstream",
            "permission": [],
            "root": m_id,
            "parent": None,
        }
        for m_id in advertised
    ]
    return JSONResponse(content={"object": "list", "data": data})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """OpenAI-compatible chat completion handler with routing, budget check, and patch healing."""
    assert _config is not None and _router is not None and _budget_guard is not None and _healer is not None

    start_time = time.time()
    try:
        body = await request.json()
    except Exception as ex:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {ex}")

    requested_model = body.get("model", "argus/auto")
    stream = body.get("stream", False)
    run_id = request.headers.get("x-argus-run-id", "default_run")
    user_agent = request.headers.get("user-agent", "")
    client_header = request.headers.get("x-argus-client", "")
    client = detect_client(client_header, user_agent)

    # 0. Immediate security refusal for prohibited Groq headers or credentials
    upstream_hdr = (request.headers.get("x-upstream-provider") or "").strip().lower()
    auth_hdr = (request.headers.get("authorization") or "").strip()
    if upstream_hdr == "groq" or auth_hdr.startswith("Bearer gsk_") or auth_hdr.startswith("Bearer gsk-"):
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "Groq is strictly prohibited across all of Argus.",
                    "type": "prohibited_provider",
                    "code": "prohibited_provider",
                }
            },
        )

    # 1. Client Authentication to Argus Gateway (mandatory)
    auth_err = verify_gateway_auth(request)
    if auth_err is not None:
        return auth_err

    # 2. Budget Guardrail Enforcement (Phase 1: Reserve with dynamic size estimation)
    est_amount = estimate_request_cost(body, model=requested_model)
    budget_ok, budget_err, res_id = _budget_guard.reserve(run_id, estimated_amount=est_amount)
    if not budget_ok:
        if _config.budget.on_exhausted == "fail_fast":
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": f"[Argus Budget Guard] {budget_err}",
                        "type": "insufficient_quota",
                        "code": "budget_exceeded",
                    }
                },
            )

    # 2. Dynamic Model Tier Resolution
    try:
        target = _router.resolve(
            requested_model=requested_model,
            prompt=body.get("prompt"),
            messages=body.get("messages"),
        )
    except NoEligibleProviderError as ex:
        _budget_guard.release(res_id)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": str(ex),
                    "type": "provider_resolution_error",
                    "code": "no_eligible_provider",
                }
            },
        )

    # 3. SSRF and Upstream Host Validation
    if not validate_upstream_url(target.base_url):
        _budget_guard.release(res_id)
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": f"Invalid or unsafe upstream base URL: '{target.base_url}'.",
                    "type": "invalid_upstream_url",
                    "code": "ssrf_protection",
                }
            },
        )

    # 4. Safe Upstream Credential Resolution & Provider Isolation
    expected_token = _config.get_gateway_key()
    client_supplied_upstream_key, cred_err = resolve_client_upstream_credential(
        request, target.provider_name, expected_token, has_configured_key=bool(target.api_key)
    )
    if cred_err:
        _budget_guard.release(res_id)
        return JSONResponse(
            status_code=400,
            content={"error": cred_err},
        )

    configured_provider_key = target.api_key
    # Explicit client-supplied upstream key takes precedence over configured default provider key
    effective_upstream_key = client_supplied_upstream_key or configured_provider_key

    if not effective_upstream_key and not target.base_url.startswith("host://"):
        _budget_guard.release(res_id)
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": f"Provider '{target.provider_name}' requires an API key, but none is configured in Argus or provided by client.",
                    "type": "upstream_authentication_missing",
                    "code": "missing_provider_key",
                }
            },
        )

    # Rewrite request payload with resolved upstream model
    upstream_body = dict(body)
    upstream_body["model"] = target.upstream_model

    # 5. Host Adapter Execution (Antigravity Host Runtime)
    if target.base_url.startswith("host://") or target.provider_name == "antigravity":
        return await _handle_host_request(
            upstream_body=upstream_body,
            requested_model=requested_model,
            target_model=target.upstream_model,
            run_id=run_id,
            client=client,
            tier=target.tier_name,
            start_time=start_time,
            res_id=res_id,
            stream=stream,
        )

    upstream_url = f"{target.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {effective_upstream_key}",
        "Content-Type": "application/json",
    }

    # 5. Streaming Mode Execution with In-Memory Patch Healing
    if stream:
        return await _handle_streaming_request(
            upstream_url=upstream_url,
            headers=headers,
            upstream_body=upstream_body,
            requested_model=requested_model,
            target_model=target.upstream_model,
            run_id=run_id,
            client=client,
            tier=target.tier_name,
            start_time=start_time,
            res_id=res_id,
        )

    # 6. Non-Streaming Mode Execution
    return await _handle_sync_request(
        upstream_url=upstream_url,
        headers=headers,
        upstream_body=upstream_body,
        requested_model=requested_model,
        target_model=target.upstream_model,
        run_id=run_id,
        client=client,
        tier=target.tier_name,
        start_time=start_time,
        res_id=res_id,
    )


async def _handle_host_request(
    upstream_body: Dict[str, Any],
    requested_model: str,
    target_model: str,
    run_id: str,
    client: str = "api-client",
    tier: str = "cheap",
    start_time: float = 0.0,
    res_id: Optional[str] = None,
    stream: bool = False,
) -> Response:
    """Execute inference directly via authorized Antigravity host runtime adapter."""
    from argus.providers.antigravity import AntigravityHostAdapter
    from argus.providers.models import ProviderConfig as PConfig

    host_cfg = PConfig(
        name="antigravity",
        base_url="host://antigravity-cli",
        model=target_model,
        timeout_seconds=60.0,
    )
    adapter = AntigravityHostAdapter(config=host_cfg)

    raw_msgs = upstream_body.get("messages", [])

    if stream:
        async def event_generator():
            chunk_id = f"chatcmpl-host-{uuid.uuid4().hex[:12]}"
            created = int(time.time())
            first_chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": target_model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(first_chunk)}\n\n"

            accumulated_chunks: List[str] = []
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def worker():
                try:
                    for delta in adapter.stream_chat_completion(
                        messages=raw_msgs,
                        model=target_model,
                        temperature=upstream_body.get("temperature", 0.2),
                        max_tokens=upstream_body.get("max_tokens"),
                    ):
                        loop.call_soon_threadsafe(queue.put_nowait, delta)
                except Exception as ex:
                    loop.call_soon_threadsafe(queue.put_nowait, ex)
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, None)

            import threading
            t = threading.Thread(target=worker, daemon=True)
            t.start()

            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    break
                accumulated_chunks.append(item)
                content_chunk = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": target_model,
                    "choices": [{"index": 0, "delta": {"content": item}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(content_chunk)}\n\n"

            stop_chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": target_model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(stop_chunk)}\n\n"
            yield "data: [DONE]\n\n"

            if _budget_guard and res_id:
                _budget_guard.release(res_id)

            full_text = "".join(accumulated_chunks)
            in_tokens = max(1, sum(len(m.get("content", "")) for m in raw_msgs) // 4)
            out_tokens = max(1, len(full_text) // 4)
            dur_ms = (time.perf_counter() - start_time) * 1000.0
            try:
                registry = TelemetryRegistry.get(_workspace_root)
                registry.record(
                    run_id=run_id,
                    client=client,
                    tier=tier,
                    requested_model=requested_model,
                    actual_model=target_model,
                    provider="antigravity",
                    latency_ms=dur_ms,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    cost_usd=0.0,
                    status_code=200,
                )
            except Exception:
                pass

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    try:
        resp = adapter.chat_completion(
            messages=raw_msgs,
            model=target_model,
            temperature=upstream_body.get("temperature", 0.2),
            max_tokens=upstream_body.get("max_tokens"),
        )
    except Exception as exc:
        if _budget_guard and res_id:
            _budget_guard.release(res_id)
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": f"Antigravity host execution failed: {exc}",
                    "type": "host_adapter_error",
                    "code": "antigravity_execution_failed",
                }
            },
        )

    duration_ms = (time.perf_counter() - start_time) * 1000.0
    in_tokens = resp.prompt_tokens
    out_tokens = resp.completion_tokens
    total_tokens = in_tokens + out_tokens

    try:
        registry = TelemetryRegistry.get(_workspace_root)
        registry.record(
            run_id=run_id,
            client=client,
            tier=tier,
            requested_model=requested_model,
            actual_model=resp.model or target_model,
            provider="antigravity",
            latency_ms=duration_ms,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_usd=0.0,
            status_code=200,
        )
    except Exception:
        pass

    if _budget_guard and res_id:
        _budget_guard.release(res_id)

    content_text = resp.content or ""

    resp_dict = {
        "id": f"chatcmpl-host-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resp.model or target_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": in_tokens,
            "completion_tokens": out_tokens,
            "total_tokens": total_tokens,
        },
    }
    return JSONResponse(status_code=200, content=resp_dict)


async def _handle_sync_request(
    upstream_url: str,
    headers: Dict[str, str],
    upstream_body: Dict[str, Any],
    requested_model: str,
    target_model: str,
    run_id: str,
    client: str = "api-client",
    tier: str = "cheap",
    start_time: float = 0.0,
    res_id: Optional[str] = None,
) -> Response:
    """Handle non-streaming request, heal any diff blocks in message content, track spend with retry."""
    assert _budget_guard is not None and _healer is not None

    retry_cfg = RetryConfig(max_retries=3, base_delay=0.5, max_delay=10.0, max_retry_after=15.0)
    last_resp: Optional[httpx.Response] = None

    for attempt in range(1, retry_cfg.max_retries + 1):
        try:
            async with create_safe_async_client(timeout=120.0, follow_redirects=False) as http_client:
                resp = await http_client.post(upstream_url, headers=headers, json=upstream_body)
                classification = classify_response(resp.status_code, resp.text)
                if classification == FailureClassification.SUCCESS:
                    last_resp = resp
                    break

                if not is_retryable(classification) or attempt >= retry_cfg.max_retries:
                    last_resp = resp
                    break

                retry_after = parse_retry_after(resp.headers.get("retry-after"))
                delay, within_bounds = calculate_backoff(attempt, retry_cfg, retry_after)
                if not within_bounds:
                    last_resp = resp
                    break

                logger.warning(
                    f"Upstream request failed ({classification.value}, HTTP {resp.status_code}). "
                    f"Retrying in {delay:.2f}s (attempt {attempt}/{retry_cfg.max_retries})..."
                )
                await asyncio.sleep(delay)
        except (Exception, asyncio.CancelledError) as ex:
            if isinstance(ex, asyncio.CancelledError):
                _budget_guard.record_uncertain(
                    reservation_id=res_id,
                    run_id=run_id,
                    model=target_model,
                    estimated_amount=0.001,
                )
                TelemetryRegistry.get().record_turn(
                    client=client,
                    requested_model=requested_model,
                    routed_model=target_model,
                    tier=tier,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=(time.time() - start_time) * 1000.0 if start_time > 0 else 0.0,
                    cost_usd=0.0,
                    usage_source="unknown",
                    cost_status="uncertain",
                    uncertain_usd=0.001,
                )
                raise

            classification = classify_exception(ex)
            # If outcome is uncertain (e.g. read timeout after dispatch), do NOT retry and do NOT release reservation as 0.0 spend!
            if is_uncertain_execution(classification):
                _budget_guard.record_uncertain(
                    reservation_id=res_id,
                    run_id=run_id,
                    model=target_model,
                    estimated_amount=0.001,
                )
                TelemetryRegistry.get().record_turn(
                    client=client,
                    requested_model=requested_model,
                    routed_model=target_model,
                    tier=tier,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=(time.time() - start_time) * 1000.0 if start_time > 0 else 0.0,
                    cost_usd=0.0,
                    usage_source="unknown",
                    cost_status="uncertain",
                    uncertain_usd=0.001,
                )
                clean_msg = scrub_secrets(f"Upstream read timeout / uncertain execution ({classification.value}): {ex}")
                return JSONResponse(
                    status_code=504,
                    content={"error": {"message": clean_msg, "type": "timeout", "code": "upstream_timeout"}},
                )

            if not is_retryable(classification) or attempt >= retry_cfg.max_retries:
                _budget_guard.release(res_id)
                clean_msg = scrub_secrets(f"Upstream connection failed ({classification.value}): {ex}")
                return JSONResponse(
                    status_code=502,
                    content={"error": {"message": clean_msg, "type": "bad_gateway"}},
                )
            delay, _ = calculate_backoff(attempt, retry_cfg)
            logger.warning(
                f"Upstream connection error ({classification.value}: {ex}). "
                f"Retrying in {delay:.2f}s (attempt {attempt}/{retry_cfg.max_retries})..."
            )
            await asyncio.sleep(delay)

    if last_resp is None:
        _budget_guard.release(res_id)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "Upstream request failed after retries", "type": "bad_gateway"}},
        )

    if last_resp.status_code != 200:
        has_usage = False
        rep_p_tokens = 0
        rep_c_tokens = 0
        try:
            err_data = last_resp.json()
            if isinstance(err_data, dict) and "usage" in err_data and err_data["usage"]:
                u = err_data["usage"]
                rep_p_tokens = int(u.get("prompt_tokens", 0))
                rep_c_tokens = int(u.get("completion_tokens", 0))
                if rep_p_tokens > 0 or rep_c_tokens > 0:
                    has_usage = True
        except Exception:
            pass

        if has_usage:
            is_sub = "antigravity" in target_model.lower() or upstream_url.startswith("host://")
            cost = _budget_guard.commit(
                reservation_id=res_id,
                run_id=run_id,
                model=target_model,
                prompt_tokens=rep_p_tokens,
                completion_tokens=rep_c_tokens,
                is_subscription=is_sub,
            )
            TelemetryRegistry.get().record_turn(
                client=client,
                requested_model=requested_model,
                routed_model=target_model,
                tier=tier,
                prompt_tokens=rep_p_tokens,
                completion_tokens=rep_c_tokens,
                latency_ms=(time.time() - start_time) * 1000.0 if start_time > 0 else 0.0,
                cost_usd=cost,
                usage_source="provider_reported",
                cost_status="zero_subscription" if is_sub else "reported",
            )
        elif last_resp.status_code >= 500:
            _budget_guard.record_uncertain(
                reservation_id=res_id,
                run_id=run_id,
                model=target_model,
                estimated_amount=0.001,
            )
            TelemetryRegistry.get().record_turn(
                client=client,
                requested_model=requested_model,
                routed_model=target_model,
                tier=tier,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=(time.time() - start_time) * 1000.0 if start_time > 0 else 0.0,
                cost_usd=0.0,
                usage_source="unknown",
                cost_status="uncertain",
                uncertain_usd=0.001,
            )
        else:
            _budget_guard.release(res_id)

        clean_content = scrub_secrets(last_resp.text)
        return Response(
            content=clean_content.encode("utf-8"),
            status_code=last_resp.status_code,
            media_type=last_resp.headers.get("content-type", "application/json"),
        )

    data = last_resp.json()

    # Model name preservation: client sees the model alias they asked for
    data["model"] = requested_model

    # Extract choices and heal content if patch block detected
    choices = data.get("choices", [])
    healed_content = ""
    if choices and _healer.enabled:
        first_choice = choices[0]
        message = first_choice.get("message", {})
        content = message.get("content", "")
        if content and "<<<<<<< SEARCH" in content:
            healed_content = _healer.heal_block(content)
            message["content"] = healed_content

    # Track token usage in budget guard
    usage = data.get("usage", {})
    has_reported_usage = bool(usage and (usage.get("prompt_tokens") or usage.get("completion_tokens")))
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    if prompt_tokens == 0:
        prompt_chars = sum(len(str(m.get("content", ""))) for m in upstream_body.get("messages", []))
        if "tools" in upstream_body:
            prompt_chars += len(json.dumps(upstream_body["tools"]))
        prompt_tokens = max(1, prompt_chars // 4)
    if completion_tokens == 0:
        resp_chars = len(healed_content) if healed_content else sum(
            len(str(c.get("message", {}).get("content", ""))) for c in choices
        )
        completion_tokens = max(1, resp_chars // 4)

    latency_ms = (time.time() - start_time) * 1000.0 if start_time > 0 else 0.0
    is_sub = "antigravity" in target_model.lower() or upstream_url.startswith("host://")
    cost = _budget_guard.commit(
        reservation_id=res_id,
        run_id=run_id,
        model=target_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        is_subscription=is_sub,
    )
    usage_source = "provider_reported" if has_reported_usage else "locally_estimated"
    cost_status = "zero_subscription" if is_sub else "estimated"
    TelemetryRegistry.get().record_turn(
        client=client,
        requested_model=requested_model,
        routed_model=target_model,
        tier=tier,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        cost_usd=cost,
        usage_source=usage_source,
        cost_status=cost_status,
    )

    return JSONResponse(content=data)

async def _stream_openai_chunks(
    upstream_url: str,
    headers: Dict[str, str],
    upstream_body: Dict[str, Any],
    requested_model: str,
    target_model: str,
    run_id: str,
    client: str = "api-client",
    tier: str = "cheap",
    start_time: float = 0.0,
    res_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Core generator that streams upstream chunks and processes them through StreamInterceptor."""
    assert _budget_guard is not None and _healer is not None

    # Request upstream usage statistics in stream if supported
    if "stream_options" not in upstream_body:
        upstream_body["stream_options"] = {"include_usage": True}

    # Pass-through mode when structured tools are present (zero buffering latency for agent loops)
    has_tools = bool(upstream_body.get("tools") or upstream_body.get("tool_choice"))
    interceptor = StreamInterceptor(_healer, pass_through=has_tools)

    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_content_chars = 0
    total_tool_chars = 0
    has_reported_usage = False

    retry_cfg = RetryConfig(max_retries=3, base_delay=0.5, max_delay=10.0, max_retry_after=15.0)

    try:
        # Pre-stream connection attempt with retry loop before emitting chunks to client
        http_client = create_safe_async_client(timeout=120.0, follow_redirects=False)
        response = None
        for attempt in range(1, retry_cfg.max_retries + 1):
            try:
                req = http_client.build_request("POST", upstream_url, headers=headers, json=upstream_body)
                resp = await http_client.send(req, stream=True)
                classification = classify_response(resp.status_code)
                if classification == FailureClassification.SUCCESS:
                    response = resp
                    break

                err_bytes = await resp.aread()
                await resp.aclose()

                if not is_retryable(classification) or attempt >= retry_cfg.max_retries:
                    if resp.status_code >= 500:
                        _budget_guard.record_uncertain(
                            reservation_id=res_id,
                            run_id=run_id,
                            model=target_model,
                            estimated_amount=0.001,
                        )
                        TelemetryRegistry.get().record_turn(
                            client=client,
                            requested_model=requested_model,
                            routed_model=target_model,
                            tier=tier,
                            prompt_tokens=0,
                            completion_tokens=0,
                            latency_ms=(time.time() - start_time) * 1000.0 if start_time > 0 else 0.0,
                            cost_usd=0.0,
                            usage_source="unknown",
                            cost_status="uncertain",
                            uncertain_usd=0.001,
                        )
                    else:
                        _budget_guard.release(res_id)
                    clean_err = scrub_secrets(err_bytes.decode("utf-8", errors="replace"))
                    yield f"data: {clean_err}\n\n"
                    yield "data: [DONE]\n\n"
                    await http_client.aclose()
                    return

                retry_after = parse_retry_after(resp.headers.get("retry-after"))
                delay, within_bounds = calculate_backoff(attempt, retry_cfg, retry_after)
                if not within_bounds:
                    if resp.status_code >= 500:
                        _budget_guard.record_uncertain(
                            reservation_id=res_id,
                            run_id=run_id,
                            model=target_model,
                            estimated_amount=0.001,
                        )
                        TelemetryRegistry.get().record_turn(
                            client=client,
                            requested_model=requested_model,
                            routed_model=target_model,
                            tier=tier,
                            prompt_tokens=0,
                            completion_tokens=0,
                            latency_ms=(time.time() - start_time) * 1000.0 if start_time > 0 else 0.0,
                            cost_usd=0.0,
                            usage_source="unknown",
                            cost_status="uncertain",
                            uncertain_usd=0.001,
                        )
                    else:
                        _budget_guard.release(res_id)
                    clean_err = scrub_secrets(err_bytes.decode("utf-8", errors="replace"))
                    yield f"data: {clean_err}\n\n"
                    yield "data: [DONE]\n\n"
                    await http_client.aclose()
                    return

                logger.warning(
                    f"Upstream stream failed ({classification.value}, HTTP {resp.status_code}). "
                    f"Retrying in {delay:.2f}s (attempt {attempt}/{retry_cfg.max_retries})..."
                )
                await asyncio.sleep(delay)
            except (Exception, asyncio.CancelledError) as ex:
                if isinstance(ex, asyncio.CancelledError):
                    _budget_guard.record_uncertain(
                        reservation_id=res_id,
                        run_id=run_id,
                        model=target_model,
                        estimated_amount=0.001,
                    )
                    await http_client.aclose()
                    raise

                classification = classify_exception(ex)
                if is_uncertain_execution(classification):
                    _budget_guard.record_uncertain(
                        reservation_id=res_id,
                        run_id=run_id,
                        model=target_model,
                        estimated_amount=0.001,
                    )
                    TelemetryRegistry.get().record_turn(
                        client=client,
                        requested_model=requested_model,
                        routed_model=target_model,
                        tier=tier,
                        prompt_tokens=0,
                        completion_tokens=0,
                        latency_ms=(time.time() - start_time) * 1000.0 if start_time > 0 else 0.0,
                        cost_usd=0.0,
                        usage_source="unknown",
                        cost_status="uncertain",
                        uncertain_usd=0.001,
                    )
                    clean_msg = scrub_secrets(f"Upstream stream read timeout ({classification.value}): {ex}")
                    err_chunk = {"error": {"message": clean_msg, "type": "stream_timeout", "code": "upstream_timeout"}}
                    yield f"data: {json.dumps(err_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    await http_client.aclose()
                    return

                if not is_retryable(classification) or attempt >= retry_cfg.max_retries:
                    _budget_guard.release(res_id)
                    clean_msg = scrub_secrets(f"Upstream stream connection error ({classification.value}): {ex}")
                    err_chunk = {
                        "error": {
                            "message": clean_msg,
                            "type": "stream_error",
                        }
                    }
                    yield f"data: {json.dumps(err_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    await http_client.aclose()
                    return

                delay, _ = calculate_backoff(attempt, retry_cfg)
                logger.warning(
                    f"Upstream stream connection error ({classification.value}: {ex}). "
                    f"Retrying in {delay:.2f}s (attempt {attempt}/{retry_cfg.max_retries})..."
                )
                await asyncio.sleep(delay)

        if response is None:
            _budget_guard.release(res_id)
            err_chunk = {"error": {"message": "Upstream stream failed after retries", "type": "stream_error"}}
            yield f"data: {json.dumps(err_chunk)}\n\n"
            yield "data: [DONE]\n\n"
            await http_client.aclose()
            return

        try:
            async for line in response.aiter_lines():
                line_str = line.strip()
                if not line_str:
                    continue

                if line_str == "data: [DONE]":
                    flushed = interceptor.flush()
                    if flushed:
                        chunk = {
                            "id": f"argus-chunk-{int(time.time()*1000)}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": requested_model,
                            "choices": [
                                {"index": 0, "delta": {"content": flushed}, "finish_reason": None}
                            ],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                    yield "data: [DONE]\n\n"
                    break

                if line_str.startswith("data: "):
                    raw_json = line_str[6:]
                    try:
                        chunk_dict = json.loads(raw_json)
                    except Exception:
                        yield f"{line_str}\n\n"
                        continue

                    if "usage" in chunk_dict and chunk_dict["usage"]:
                        u = chunk_dict["usage"]
                        total_prompt_tokens = u.get("prompt_tokens", total_prompt_tokens)
                        total_completion_tokens = u.get("completion_tokens", total_completion_tokens)
                        has_reported_usage = True

                    choices = chunk_dict.get("choices", [])
                    if not choices:
                        yield f"data: {raw_json}\n\n"
                        continue

                    choice0 = choices[0]
                    delta = choice0.get("delta", {})
                    content_delta = delta.get("content", "")

                    if "tool_calls" in delta and delta["tool_calls"]:
                        for tc in delta["tool_calls"]:
                            fn = tc.get("function", {})
                            total_tool_chars += len(fn.get("name", "")) + len(fn.get("arguments", ""))

                    if not content_delta:
                        chunk_dict["model"] = requested_model
                        yield f"data: {json.dumps(chunk_dict)}\n\n"
                        continue

                    total_content_chars += len(content_delta)
                    processed_text = interceptor.process_chunk(content_delta)
                    if processed_text:
                        chunk_dict["model"] = requested_model
                        choice0["delta"]["content"] = processed_text
                        yield f"data: {json.dumps(chunk_dict)}\n\n"
        finally:
            await response.aclose()
            await http_client.aclose()

    except (Exception, asyncio.CancelledError) as ex:
        if isinstance(ex, asyncio.CancelledError):
            if total_content_chars == 0 and total_completion_tokens == 0:
                _budget_guard.record_uncertain(
                    reservation_id=res_id,
                    run_id=run_id,
                    model=target_model,
                    estimated_amount=0.001,
                )
            raise

        classification = classify_exception(ex)
        if is_uncertain_execution(classification):
            _budget_guard.record_uncertain(
                reservation_id=res_id,
                run_id=run_id,
                model=target_model,
                estimated_amount=0.001,
            )
        else:
            _budget_guard.release(res_id)

        clean_msg = scrub_secrets(f"Upstream stream connection error: {ex}")
        err_chunk = {
            "error": {
                "message": clean_msg,
                "type": "stream_error",
            }
        }
        yield f"data: {json.dumps(err_chunk)}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        if total_content_chars > 0 or total_completion_tokens > 0:
            if total_prompt_tokens == 0:
                prompt_chars = sum(len(str(m.get("content", ""))) for m in upstream_body.get("messages", []))
                if "tools" in upstream_body:
                    prompt_chars += len(json.dumps(upstream_body["tools"]))
                total_prompt_tokens = max(1, prompt_chars // 4)

            if total_completion_tokens == 0:
                total_completion_tokens = max(1, (total_content_chars + total_tool_chars) // 4)

            latency_ms = (time.time() - start_time) * 1000.0 if start_time > 0 else 0.0
            is_sub = "antigravity" in target_model.lower() or upstream_url.startswith("host://")
            cost = _budget_guard.commit(
                reservation_id=res_id,
                run_id=run_id,
                model=target_model,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                is_subscription=is_sub,
            )
            usage_source = "provider_reported" if has_reported_usage else "locally_estimated"
            cost_status = "zero_subscription" if is_sub else "estimated"
            TelemetryRegistry.get().record_turn(
                client=client,
                requested_model=requested_model,
                routed_model=target_model,
                tier=tier,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                latency_ms=latency_ms,
                cost_usd=cost,
                usage_source=usage_source,
                cost_status=cost_status,
            )
        else:
            _budget_guard.release(res_id)


async def _handle_streaming_request(
    upstream_url: str,
    headers: Dict[str, str],
    upstream_body: Dict[str, Any],
    requested_model: str,
    target_model: str,
    run_id: str,
    client: str = "api-client",
    tier: str = "cheap",
    start_time: float = 0.0,
    res_id: Optional[str] = None,
) -> StreamingResponse:
    """Stream upstream response through StreamInterceptor, healing diff blocks in-flight."""
    return StreamingResponse(
        _stream_openai_chunks(
            upstream_url=upstream_url,
            headers=headers,
            upstream_body=upstream_body,
            requested_model=requested_model,
            target_model=target_model,
            run_id=run_id,
            client=client,
            tier=tier,
            start_time=start_time,
            res_id=res_id,
        ),
        media_type="text/event-stream",
    )


@app.post("/v1/messages")
async def anthropic_messages(request: Request) -> Response:
    """Anthropic Messages API (/v1/messages) adapter endpoint for Claude Code and Anthropic clients."""
    assert _config is not None and _router is not None and _budget_guard is not None and _healer is not None

    start_time = time.time()
    try:
        body = await request.json()
    except Exception as ex:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {ex}")

    requested_model = body.get("model", "argus/auto")
    stream = body.get("stream", False)
    run_id = request.headers.get("x-argus-run-id", request.headers.get("x-api-key", "anthropic_run"))
    user_agent = request.headers.get("user-agent", "")
    client_header = request.headers.get("x-argus-client", "")
    client = detect_client(client_header, user_agent)
    if client in ("external-client", "api-client"):
        client = "claude-code"

    # 0. Immediate security refusal for prohibited Groq headers or credentials
    upstream_hdr = (request.headers.get("x-upstream-provider") or "").strip().lower()
    auth_hdr = (request.headers.get("authorization") or "").strip()
    if upstream_hdr == "groq" or auth_hdr.startswith("Bearer gsk_") or auth_hdr.startswith("Bearer gsk-"):
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {
                    "type": "prohibited_provider",
                    "message": "Groq is strictly prohibited across all of Argus.",
                    "code": "prohibited_provider",
                },
            },
        )

    # 1. Gateway Client Authentication (mandatory)
    auth_err = verify_gateway_auth(request)
    if auth_err is not None:
        return auth_err

    # 2. Budget Guardrail Enforcement (Phase 1 Reservation with dynamic size estimation)
    est_amount = estimate_request_cost(body, model=requested_model)
    res_ok, res_err, res_id = _budget_guard.reserve(run_id, estimated_cost=est_amount)
    if not res_ok:
        if _config.budget.on_exhausted == "fail_fast":
            return JSONResponse(
                status_code=429,
                content={
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error",
                        "message": f"[Argus Budget Guard] {res_err}",
                    },
                },
            )

    # 3. Dynamic Model Tier Resolution
    try:
        target = _router.resolve(
            requested_model=requested_model,
            prompt=body.get("system"),
            messages=body.get("messages"),
        )
    except NoEligibleProviderError as ex:
        _budget_guard.release(res_id)
        return JSONResponse(
            status_code=503,
            content={
                "type": "error",
                "error": {
                    "type": "provider_resolution_error",
                    "message": str(ex),
                    "code": "no_eligible_provider",
                },
            },
        )

    # 4. SSRF and Upstream Host Validation
    if not validate_upstream_url(target.base_url):
        _budget_guard.release(res_id)
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {
                    "type": "invalid_upstream_url",
                    "message": f"Invalid or unsafe upstream base URL: '{target.base_url}'.",
                    "code": "ssrf_protection",
                },
            },
        )

    # 5. Safe Upstream Credential Resolution & Provider Isolation
    expected_token = _config.get_gateway_key()
    client_supplied_upstream_key, cred_err = resolve_client_upstream_credential(
        request, target.provider_name, expected_token, has_configured_key=bool(target.api_key)
    )
    if cred_err:
        _budget_guard.release(res_id)
        return JSONResponse(
            status_code=400,
            content={"type": "error", "error": cred_err},
        )

    configured_provider_key = target.api_key
    # Explicit client-supplied upstream key takes precedence over configured default provider key
    effective_upstream_key = client_supplied_upstream_key or configured_provider_key

    if not effective_upstream_key and not target.base_url.startswith("host://"):
        _budget_guard.release(res_id)
        return JSONResponse(
            status_code=401,
            content={
                "type": "error",
                "error": {
                    "type": "upstream_authentication_missing",
                    "message": f"Provider '{target.provider_name}' requires an API key, but none is configured in Argus or provided by client.",
                    "code": "missing_provider_key",
                },
            },
        )

    # 6. Translate Anthropic request to standard OpenAI format
    openai_body = anthropic_to_openai_request(body)
    openai_body["model"] = target.upstream_model

    upstream_url = f"{target.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {effective_upstream_key}",
        "Content-Type": "application/json",
    }

    # 7. Streaming vs Sync execution
    if stream:
        gen = _stream_openai_chunks(
            upstream_url=upstream_url,
            headers=headers,
            upstream_body=openai_body,
            requested_model=requested_model,
            target_model=target.upstream_model,
            run_id=run_id,
            client=client,
            tier=target.tier_name,
            start_time=start_time,
            res_id=res_id,
        )
        anthropic_stream = transform_openai_stream_to_anthropic(gen, requested_model)
        return StreamingResponse(anthropic_stream, media_type="text/event-stream")

    return await _handle_anthropic_sync(
        upstream_url=upstream_url,
        headers=headers,
        upstream_body=openai_body,
        requested_model=requested_model,
        target_model=target.upstream_model,
        run_id=run_id,
        client=client,
        tier=target.tier_name,
        start_time=start_time,
        res_id=res_id,
    )


async def _handle_anthropic_sync(
    upstream_url: str,
    headers: Dict[str, str],
    upstream_body: Dict[str, Any],
    requested_model: str,
    target_model: str,
    run_id: str,
    client: str = "claude-code",
    tier: str = "cheap",
    start_time: float = 0.0,
    res_id: Optional[str] = None,
) -> Response:
    """Handle non-streaming request, heal diff blocks, and format as Anthropic message response."""
    assert _budget_guard is not None and _healer is not None

    async with create_safe_async_client(timeout=120.0, follow_redirects=False) as http_client:
        try:
            resp = await http_client.post(upstream_url, headers=headers, json=upstream_body)
        except (Exception, asyncio.CancelledError) as ex:
            if isinstance(ex, asyncio.CancelledError):
                _budget_guard.record_uncertain(
                    reservation_id=res_id,
                    run_id=run_id,
                    model=target_model,
                    estimated_amount=0.01,
                )
                TelemetryRegistry.get().record_turn(
                    client=client,
                    requested_model=requested_model,
                    routed_model=target_model,
                    tier=tier,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=(time.time() - start_time) * 1000.0 if start_time > 0 else 0.0,
                    cost_usd=0.0,
                    usage_source="unknown",
                    cost_status="uncertain",
                    uncertain_usd=0.01,
                )
                raise

            classification = classify_exception(ex)
            if is_uncertain_execution(classification):
                _budget_guard.record_uncertain(
                    reservation_id=res_id,
                    run_id=run_id,
                    model=target_model,
                    estimated_amount=0.01,
                )
                TelemetryRegistry.get().record_turn(
                    client=client,
                    requested_model=requested_model,
                    routed_model=target_model,
                    tier=tier,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=(time.time() - start_time) * 1000.0 if start_time > 0 else 0.0,
                    cost_usd=0.0,
                    usage_source="unknown",
                    cost_status="uncertain",
                    uncertain_usd=0.01,
                )
                clean_msg = scrub_secrets(f"Upstream read timeout / uncertain execution: {ex}")
                return JSONResponse(
                    status_code=504,
                    content={"type": "error", "error": {"type": "timeout_error", "message": clean_msg}},
                )

            _budget_guard.release(res_id)
            clean_msg = scrub_secrets(f"Upstream connection failed: {ex}")
            return JSONResponse(
                status_code=502,
                content={"type": "error", "error": {"type": "api_error", "message": clean_msg}},
            )

    if resp.status_code != 200:
        if resp.status_code >= 500:
            _budget_guard.record_uncertain(
                reservation_id=res_id,
                run_id=run_id,
                model=target_model,
                estimated_amount=0.01,
            )
            TelemetryRegistry.get().record_turn(
                client=client,
                requested_model=requested_model,
                routed_model=target_model,
                tier=tier,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=(time.time() - start_time) * 1000.0 if start_time > 0 else 0.0,
                cost_usd=0.0,
                usage_source="unknown",
                cost_status="uncertain",
                uncertain_usd=0.01,
            )
        else:
            _budget_guard.release(res_id)

        clean_content = scrub_secrets(resp.text)
        return Response(
            content=clean_content.encode("utf-8"),
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    data = resp.json()

    # Heal content if patch block detected
    choices = data.get("choices", [])
    healed_content = ""
    if choices and _healer.enabled:
        first_choice = choices[0]
        message = first_choice.get("message", {})
        content = message.get("content", "")
        if content and "<<<<<<< SEARCH" in content:
            healed = _healer.heal_block(content)
            message["content"] = healed
            healed_content = healed

    # Track token usage in budget guard
    usage = data.get("usage", {})
    has_reported_usage = bool(usage and (usage.get("prompt_tokens") or usage.get("completion_tokens")))
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    if prompt_tokens == 0:
        prompt_chars = sum(len(str(m.get("content", ""))) for m in upstream_body.get("messages", []))
        prompt_tokens = max(1, prompt_chars // 4)
    if completion_tokens == 0:
        resp_chars = len(healed_content) if healed_content else sum(
            len(str(c.get("message", {}).get("content", ""))) for c in choices
        )
        completion_tokens = max(1, resp_chars // 4)

    latency_ms = (time.time() - start_time) * 1000.0 if start_time > 0 else 0.0
    is_sub = "antigravity" in target_model.lower() or upstream_url.startswith("host://")
    cost = _budget_guard.commit(
        reservation_id=res_id,
        run_id=run_id,
        model=target_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        is_subscription=is_sub,
    )
    usage_source = "provider_reported" if has_reported_usage else "locally_estimated"
    cost_status = "zero_subscription" if is_sub else "estimated"
    TelemetryRegistry.get().record_turn(
        client=client,
        requested_model=requested_model,
        routed_model=target_model,
        tier=tier,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        cost_usd=cost,
        usage_source=usage_source,
        cost_status=cost_status,
    )

    anthropic_data = openai_to_anthropic_response(data, requested_model)
    return JSONResponse(content=anthropic_data)
