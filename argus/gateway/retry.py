"""Upstream Gateway Retry Policy: Failure classification, backoff with jitter, Retry-After parsing."""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple
import httpx

logger = logging.getLogger("argus.gateway.retry")


class FailureClassification(str, Enum):
    """Classification of upstream inference failures."""
    RATE_LIMIT = "RATE_LIMIT"          # HTTP 429
    SERVER_ERROR = "SERVER_ERROR"      # HTTP 500, 502, 503, 504
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"# Pre-dispatch connection failure (safe to retry)
    READ_TIMEOUT = "READ_TIMEOUT"      # Post-dispatch read timeout (uncertain execution)
    TIMEOUT = "TIMEOUT"                # Generic timeout
    TRANSPORT_ERROR = "TRANSPORT_ERROR"# Network drop / connection reset
    EMPTY_RESPONSE = "EMPTY_RESPONSE"  # HTTP 200 with 0 bytes or empty choice
    CLIENT_ERROR = "CLIENT_ERROR"      # HTTP 400, 401, 403, 404 (non-retryable)
    SUCCESS = "SUCCESS"


@dataclass
class RetryConfig:
    max_retries: int = 3
    base_delay: float = 0.5
    max_delay: float = 10.0
    jitter: float = 0.25
    max_retry_after: float = 15.0  # Fail fast if provider demands >15s wait
    retry_read_timeouts: bool = False  # Avoid duplicate billing by default


def classify_response(
    status_code: int,
    content: Optional[str] = None,
) -> FailureClassification:
    """Classify an HTTP response into retryable or non-retryable failure classes."""
    if status_code == 200:
        if content is not None and not content.strip():
            return FailureClassification.EMPTY_RESPONSE
        return FailureClassification.SUCCESS
    if status_code == 429:
        return FailureClassification.RATE_LIMIT
    if status_code in (500, 502, 503, 504):
        return FailureClassification.SERVER_ERROR
    if 400 <= status_code < 500:
        return FailureClassification.CLIENT_ERROR
    return FailureClassification.SERVER_ERROR


def classify_exception(exc: Exception) -> FailureClassification:
    """Classify an exception raised during HTTP execution."""
    if isinstance(exc, httpx.ConnectTimeout):
        return FailureClassification.CONNECT_TIMEOUT
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout)):
        return FailureClassification.READ_TIMEOUT
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return FailureClassification.TIMEOUT
    if isinstance(exc, (httpx.NetworkError, httpx.ConnectError, ConnectionError)):
        return FailureClassification.TRANSPORT_ERROR
    return FailureClassification.SERVER_ERROR


def is_uncertain_execution(classification: FailureClassification) -> bool:
    """Return True if the request was dispatched to upstream but response could not be verified."""
    return classification in (FailureClassification.READ_TIMEOUT,)


def is_retryable(classification: FailureClassification, retry_read_timeouts: bool = False) -> bool:
    """Determine if a failure class is safe and eligible for retry."""
    if classification == FailureClassification.READ_TIMEOUT:
        return retry_read_timeouts
    return classification in (
        FailureClassification.RATE_LIMIT,
        FailureClassification.SERVER_ERROR,
        FailureClassification.CONNECT_TIMEOUT,
        FailureClassification.TIMEOUT,
        FailureClassification.TRANSPORT_ERROR,
        FailureClassification.EMPTY_RESPONSE,
    )


def parse_retry_after(header_val: Optional[str]) -> Optional[float]:
    """Parse HTTP Retry-After header as seconds float or date delta."""
    if not header_val:
        return None
    header_val = header_val.strip()
    try:
        val = float(header_val)
        return max(0.0, val)
    except ValueError:
        pass

    try:
        parsed_tuple = email.utils.parsedate_to_datetime(header_val)
        delta = parsed_tuple.timestamp() - time.time()
        return max(0.0, delta)
    except Exception:
        return None


def calculate_backoff(
    attempt: int,
    config: RetryConfig,
    retry_after: Optional[float] = None,
) -> Tuple[float, bool]:
    """Calculate sleep delay for an attempt with exponential backoff and jitter.
    
    Returns:
        (delay_seconds, within_bounds)
    """
    if retry_after is not None:
        if retry_after > config.max_retry_after:
            return retry_after, False
        return retry_after, True

    backoff = min(config.max_delay, config.base_delay * (2 ** max(0, attempt - 1)))
    jitter_val = random.uniform(0, config.jitter)
    return backoff + jitter_val, True
