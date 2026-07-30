"""Tests for the retry policy and rate limiter."""

from __future__ import annotations

import asyncio
import datetime as dt
from email.utils import format_datetime

import httpx
import pytest

from wayback_downloader.utils.retry import (
    RATE_LIMIT_STATUS,
    RETRYABLE_STATUS,
    RateLimiter,
    RetryPolicy,
    parse_retry_after,
    retry_async,
)


def make_response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    """Build a response object attached to a dummy request."""
    return httpx.Response(
        status, headers=headers or {}, request=httpx.Request("GET", "https://example.com")
    )


def test_backoff_grows_and_is_capped() -> None:
    """Delays grow exponentially but never exceed the configured ceiling."""
    policy = RetryPolicy(max_retries=8, backoff=1.0, backoff_max=10.0, jitter=0.0)
    assert policy.delay_for(1) == pytest.approx(1.0)
    assert policy.delay_for(2) == pytest.approx(2.0)
    assert policy.delay_for(3) == pytest.approx(4.0)
    assert policy.delay_for(8) == pytest.approx(10.0)


def test_jitter_stays_within_bounds() -> None:
    """Jitter perturbs the delay without leaving the expected window."""
    policy = RetryPolicy(backoff=1.0, jitter=0.3)
    for _ in range(50):
        assert 0.7 <= policy.delay_for(1) <= 1.3


def test_rate_limit_statuses_are_retryable() -> None:
    """Every rate-limit status is also in the retryable set."""
    assert RATE_LIMIT_STATUS <= RETRYABLE_STATUS


def test_parses_numeric_retry_after() -> None:
    """A delta-seconds Retry-After header is read as a number."""
    assert parse_retry_after(make_response(429, {"Retry-After": "12"})) == pytest.approx(12.0)


def test_parses_http_date_retry_after() -> None:
    """An HTTP-date Retry-After header is converted to a delay."""
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)
    delay = parse_retry_after(make_response(503, {"Retry-After": format_datetime(future)}))
    assert delay is not None and 25 <= delay <= 31


def test_missing_or_bad_retry_after_is_none() -> None:
    """An absent or unparseable header yields no hint."""
    assert parse_retry_after(make_response(429)) is None
    assert parse_retry_after(make_response(429, {"Retry-After": "soon"})) is None


async def test_retries_until_success() -> None:
    """A transient failure is retried and the eventual result returned."""
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectTimeout("boom")
        return "ok"

    result = await retry_async(flaky, RetryPolicy(max_retries=5, backoff=0.001), "flaky")
    assert result == "ok"
    assert attempts == 3


async def test_gives_up_after_the_budget() -> None:
    """Once the retry budget is spent the last error propagates."""
    attempts = 0

    async def always_fails() -> str:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("boom")

    with pytest.raises(httpx.ConnectTimeout):
        await retry_async(always_fails, RetryPolicy(max_retries=2, backoff=0.001), "doomed")
    assert attempts == 3


async def test_non_retryable_status_fails_immediately() -> None:
    """A 404 is not retried, since repeating it cannot help."""
    attempts = 0

    async def not_found() -> str:
        nonlocal attempts
        attempts += 1
        raise httpx.HTTPStatusError(
            "404", request=httpx.Request("GET", "https://example.com"), response=make_response(404)
        )

    with pytest.raises(httpx.HTTPStatusError):
        await retry_async(not_found, RetryPolicy(max_retries=3, backoff=0.001), "missing")
    assert attempts == 1


async def test_rate_limiter_spaces_requests() -> None:
    """The limiter enforces the configured gap between acquisitions."""
    limiter = RateLimiter(min_interval=0.05)
    loop = asyncio.get_running_loop()
    start = loop.time()
    for _ in range(4):
        await limiter.acquire()
    assert loop.time() - start >= 0.1


async def test_zero_interval_does_not_block() -> None:
    """A limiter with no interval adds no measurable delay."""
    limiter = RateLimiter(min_interval=0.0)
    loop = asyncio.get_running_loop()
    start = loop.time()
    for _ in range(100):
        await limiter.acquire()
    assert loop.time() - start < 0.1
