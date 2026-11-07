"""Centralized Secret and Sensitive Data Redaction Engine for Argus.

Provides comprehensive regex-based scrubbing of API tokens, bearer tokens, passwords,
private keys, and connection URI credentials from test logs, tracebacks, prompts,
and diagnostic artifacts to prevent credential leakage.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Union


# Standard credential detection regex patterns: (pattern, replacement)
SECRET_PATTERNS = [
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
    # Groq API keys (decommissioned/prohibited)
    (r"\b(gsk_[A-Za-z0-9_\-]{16,})\b", "[REDACTED_SECRET]"),
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
