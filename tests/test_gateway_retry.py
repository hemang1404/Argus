"""Unit tests for Argus Gateway Retry Policy and Failure Classification."""

import email.utils
import time
import httpx
import pytest

from argus.gateway.retry import (
    FailureClassification,
    RetryConfig,
    calculate_backoff,
    classify_exception,
    classify_response,
    is_retryable,
    parse_retry_after,
)


def test_classify_response():
    assert classify_response(200, "hello world") == FailureClassification.SUCCESS
    assert classify_response(200, "") == FailureClassification.EMPTY_RESPONSE
    assert classify_response(200, "   ") == FailureClassification.EMPTY_RESPONSE
    assert classify_response(429) == FailureClassification.RATE_LIMIT
    assert classify_response(500) == FailureClassification.SERVER_ERROR
    assert classify_response(502) == FailureClassification.SERVER_ERROR
    assert classify_response(503) == FailureClassification.SERVER_ERROR
    assert classify_response(504) == FailureClassification.SERVER_ERROR
    assert classify_response(400) == FailureClassification.CLIENT_ERROR
    assert classify_response(401) == FailureClassification.CLIENT_ERROR
    assert classify_response(404) == FailureClassification.CLIENT_ERROR


def test_classify_exception():
    assert classify_exception(httpx.ReadTimeout("timeout")) == FailureClassification.READ_TIMEOUT
    assert classify_exception(httpx.ConnectTimeout("timeout")) == FailureClassification.CONNECT_TIMEOUT
    assert classify_exception(httpx.ConnectError("refused")) == FailureClassification.TRANSPORT_ERROR
    assert classify_exception(ConnectionResetError("reset")) == FailureClassification.TRANSPORT_ERROR
    assert classify_exception(RuntimeError("generic")) == FailureClassification.SERVER_ERROR


def test_is_retryable():
    assert is_retryable(FailureClassification.RATE_LIMIT) is True
    assert is_retryable(FailureClassification.SERVER_ERROR) is True
    assert is_retryable(FailureClassification.TIMEOUT) is True
    assert is_retryable(FailureClassification.CONNECT_TIMEOUT) is True
    assert is_retryable(FailureClassification.READ_TIMEOUT, retry_read_timeouts=False) is False
    assert is_retryable(FailureClassification.READ_TIMEOUT, retry_read_timeouts=True) is True
    assert is_retryable(FailureClassification.TRANSPORT_ERROR) is True
    assert is_retryable(FailureClassification.EMPTY_RESPONSE) is True
    assert is_retryable(FailureClassification.CLIENT_ERROR) is False
    assert is_retryable(FailureClassification.SUCCESS) is False


def test_parse_retry_after_seconds():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("invalid_header") is None


def test_parse_retry_after_http_date():
    future_time = time.time() + 10
    date_str = email.utils.formatdate(future_time, usegmt=True)
    parsed = parse_retry_after(date_str)
    assert parsed is not None
    assert 8.0 <= parsed <= 12.0


def test_calculate_backoff():
    cfg = RetryConfig(max_retries=3, base_delay=1.0, max_delay=10.0, jitter=0.2, max_retry_after=15.0)

    # Attempt 1: ~1.0 + jitter
    delay1, within1 = calculate_backoff(1, cfg)
    assert 1.0 <= delay1 <= 1.25
    assert within1 is True

    # Attempt 2: ~2.0 + jitter
    delay2, within2 = calculate_backoff(2, cfg)
    assert 2.0 <= delay2 <= 2.25
    assert within2 is True

    # Retry-After within bounds
    delay_ra, within_ra = calculate_backoff(1, cfg, retry_after=5.0)
    assert delay_ra == 5.0
    assert within_ra is True

    # Retry-After exceeding bounds
    delay_ex, within_ex = calculate_backoff(1, cfg, retry_after=60.0)
    assert delay_ex == 60.0
    assert within_ex is False
