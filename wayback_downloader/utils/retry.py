"""Retry policy with exponential backoff, jitter and rate-limit awareness."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, TypeVar

import httpx

from wayback_downloader.exceptions import RateLimitError
from wayback_downloader.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 509, 522, 524})
RATE_LIMIT_STATUS = frozenset({429, 503, 509})

RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ReadError,
)


@dataclass(frozen=True)
class RetryPolicy:
    """Parameters controlling how a failed request is retried."""

    max_retries: int = 4
    backoff: float = 0.5
    backoff_max: float = 20.0
    jitter: float = 0.3

    def delay_for(self, attempt: int) -> float:
        """Return the pre-jitter delay in seconds before the given attempt.

        ``attempt`` is 1-based: attempt 1 is the first retry.
        """
        raw = self.backoff * (2 ** (attempt - 1))
        capped = min(raw, self.backoff_max)
        spread = capped * self.jitter
        return max(0.0, capped + random.uniform(-spread, spread))


def parse_retry_after(response: httpx.Response) -> float | None:
    """Interpret a ``Retry-After`` header as a delay in seconds.

    Supports both the delta-seconds and the HTTP-date forms defined by
    RFC 9110; returns ``None`` when the header is absent or unparseable.
    """
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    import datetime as dt

    now = dt.datetime.now(tz=target.tzinfo or dt.timezone.utc)
    return max(0.0, (target - now).total_seconds())


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    description: str,
    retry_after_hint: Callable[[Exception], float | None] | None = None,
) -> T:
    """Run ``operation`` until it succeeds or the retry budget is exhausted.

    ``retry_after_hint`` lets the caller extract a server-supplied delay from a
    raised exception so that a 429 is honoured rather than blindly backed off.
    """
    last_error: Exception | None = None

    for attempt in range(policy.max_retries + 1):
        try:
            return await operation()
        except RETRYABLE_EXCEPTIONS as exc:
            last_error = exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in RETRYABLE_STATUS:
                raise
            last_error = exc
        except RateLimitError as exc:
            last_error = exc

        if attempt >= policy.max_retries:
            break

        delay = policy.delay_for(attempt + 1)
        if retry_after_hint is not None and last_error is not None:
            hinted = retry_after_hint(last_error)
            if hinted is not None:
                delay = max(delay, hinted)

        logger.debug(
            "%s failed (%s); retry %d/%d in %.2fs",
            description,
            type(last_error).__name__,
            attempt + 1,
            policy.max_retries,
            delay,
        )
        await asyncio.sleep(delay)

    assert last_error is not None
    raise last_error


class RateLimiter:
    """Async token-pacer enforcing a minimum interval between request starts.

    An interval of zero disables pacing entirely and costs one uncontended lock
    acquisition per call.
    """

    def __init__(self, min_interval: float = 0.0) -> None:
        """Configure the limiter with a minimum inter-request interval."""
        self.min_interval = max(0.0, min_interval)
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    async def acquire(self) -> None:
        """Block until the caller is allowed to issue its request."""
        if self.min_interval <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self._next_slot - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._next_slot = now + self.min_interval
