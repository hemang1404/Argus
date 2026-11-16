"""Centralized Secret and Sensitive Data Redaction Engine for Argus.

Provides comprehensive regex-based scrubbing of API tokens, bearer tokens, passwords,
private keys, and connection URI credentials from test logs, tracebacks, prompts,
and diagnostic artifacts to prevent credential leakage.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional


# Token-shaped patterns: vendor-prefixed credentials and PEM private keys. These match
# only strings that are credentials by construction, so they are safe to apply to
# source code that is sent to a model.
TOKEN_PATTERNS = [
    # 1. Private Keys (PEM blocks)
    (
        r"-----BEGIN\s+(?:[A-Z\s]+)?PRIVATE\s+KEY-----[\s\S]*?-----END\s+(?:[A-Z\s]+)?PRIVATE\s+KEY-----",
        "[REDACTED_PRIVATE_KEY]",
    ),
    # 2. Specific Known API Keys
    # OpenAI project and personal keys
    (r"\b(sk-proj-[A-Za-z0-9_\-]{20,})\b", "[REDACTED_SECRET]"),
    (r"\b(sk-[A-Za-z0-9_\-]{20,})\b", "[REDACTED_SECRET]"),
    # Anthropic API keys
    (r"\b(sk-ant-[A-Za-z0-9_\-]{20,})\b", "[REDACTED_SECRET]"),
    # GitHub Personal Access Tokens & Fine-grained tokens
    (r"\b(ghp_[A-Za-z0-9]{30,}|gho_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b", "[REDACTED_SECRET]"),
    # Google AI / Cloud API keys
    (r"\b(AIzaSy[A-Za-z0-9_\-]{25,})\b", "[REDACTED_SECRET]"),
    # AWS Access Key IDs
    (r"\b(AKIA[0-9A-Z]{16})\b", "[REDACTED_SECRET]"),
    # Hugging Face tokens
    (r"\b(hf_[A-Za-z0-9]{30,})\b", "[REDACTED_SECRET]"),
    # Slack tokens
    (r"\b(xox[baprs]-[A-Za-z0-9_\-]{10,})\b", "[REDACTED_SECRET]"),
    # Groq API keys (user-supplied; must never reach logs or persisted output)
    (r"\b(gsk_[A-Za-z0-9_\-]{16,})\b", "[REDACTED_SECRET]"),
    # Bearer credentials in HTTP-header form: a JWT-shaped (dotted) token or a long opaque one.
    # Short words after "Bearer" (prose, ``Bearer {token}`` templates) are left alone.
    (r"(\bBearer\s+)(?:[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-\.]+|[A-Za-z0-9_\-\.]{24,})", r"\1[REDACTED_SECRET]"),
]

# Context-dependent patterns: they redact whatever follows ``password=``, ``api_key:``,
# ``Bearer`` or a URI userinfo, so they also rewrite ordinary code such as
# ``password = get_password()``. Appropriate for logs, tracebacks and persisted
# artifacts, NOT for source code the model is asked to repair.
CONTEXT_PATTERNS = [
    # 3. Bearer & Authorization Headers
    (r"((?:Bearer\s+))[A-Za-z0-9_\-\.]{8,}", r"\1[REDACTED_SECRET]"),
    (
        r"((?:Authorization['\"]?\s*[:=]\s*['\"]?(?:Bearer\s+)?))[A-Za-z0-9_\-\.]{8,}",
        r"\1[REDACTED_SECRET]",
    ),
    # 4. Key-Value & Env-Style Credentials (api-key=..., password=..., secret_key: ...)
    (
        r"((?:x-api-key|api[-_]?key|secret[-_]?key|auth[-_]?token)['\"]?\s*[:=]\s*['\"]?)[^\s,'\"}\]\)>]+",
        r"\1[REDACTED_SECRET]",
    ),
    (
        r"((?:password|passwd|pwd)['\"]?\s*[:=]\s*['\"]?)[^\s,'\"}\]\)>]+",
        r"\1[REDACTED_SECRET]",
    ),
    # 5. Database Connection URIs with embedded passwords (postgres://user:pass@host)
    (
        r"((?:postgres|postgresql|mysql|mongodb|redis|amqp)://[^:]+:)([^@]+)(@)",
        r"\1[REDACTED_PASSWORD]\3",
    ),
]


SECRET_PATTERNS = TOKEN_PATTERNS + CONTEXT_PATTERNS


def scrub_prompt(text: str, keys: Optional[List[str]] = None) -> str:
    """Redact credentials from text sent to a model without altering code semantics.

    Only exact configured keys and token-shaped secrets (vendor key formats, PEM private
    keys) are replaced. Assignments such as ``password = compute()`` or ``api_key=None``
    are left intact, so the model sees the code it must repair. Use :func:`scrub_secrets`
    for logs and persisted artifacts.
    """
    if not text:
        return ""
    scrubbed = str(text)
    for k in keys or []:
        if k and len(str(k)) >= 4 and str(k) in scrubbed:
            scrubbed = scrubbed.replace(str(k), "[REDACTED_SECRET]")
    for pattern, replacement in TOKEN_PATTERNS:
        scrubbed = re.sub(pattern, replacement, scrubbed, flags=re.IGNORECASE)
    return scrubbed


def scrub_secrets(text: str, keys: Optional[List[str]] = None) -> str:
    """Scrub sensitive credentials from a string to prevent log, prompt, and artifact leakage.
    
    Args:
        text: Raw input text potentially containing secrets.
        keys: Optional list of explicit secret strings to redact unconditionally.
        
    Returns:
        Sanitized string with sensitive tokens replaced with redaction placeholders.
    """
    if not text:
        return ""
    scrubbed = str(text)

    # 1. Exact string matches for known configured keys
    if keys:
        for k in keys:
            if k and len(str(k)) >= 4 and str(k) in scrubbed:
                scrubbed = scrubbed.replace(str(k), "[REDACTED_SECRET]")

    # 2. General credential patterns
    for pattern, replacement in SECRET_PATTERNS:
        scrubbed = re.sub(pattern, replacement, scrubbed, flags=re.IGNORECASE)

    return scrubbed


def scrub_object(obj: Any, keys: Optional[List[str]] = None) -> Any:
    """Recursively scrub secrets from nested dicts, lists, and strings."""
    if isinstance(obj, str):
        return scrub_secrets(obj, keys=keys)
    elif isinstance(obj, dict):
        return {k: scrub_object(v, keys=keys) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [scrub_object(item, keys=keys) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(scrub_object(item, keys=keys) for item in obj)
    return obj
