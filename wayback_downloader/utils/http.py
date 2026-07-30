"""HTTP client construction.

Requests are shaped to look like the ones the official Wayback web app issues:
same ``User-Agent``, same ``Referer``/``Origin``, same ``Accept`` negotiation.
Connections are pooled and reused across the whole run.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

from wayback_downloader.config import Settings
from wayback_downloader.exceptions import RateLimitError
from wayback_downloader.utils.logger import get_logger
from wayback_downloader.utils.retry import (
    RATE_LIMIT_STATUS,
    RateLimiter,
    RetryPolicy,
    parse_retry_after,
    retry_async,
)

logger = get_logger(__name__)

WAYBACK_APP_ORIGIN = "https://livingatlas.arcgis.com"
WAYBACK_APP_REFERER = "https://livingatlas.arcgis.com/wayback/"


def build_headers(settings: Settings, accept: str = "*/*") -> dict[str, str]:
    """Build the browser-equivalent header set for a Wayback request."""
    return {
        "User-Agent": settings.user_agent,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": WAYBACK_APP_ORIGIN,
        "Referer": WAYBACK_APP_REFERER,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "Connection": "keep-alive",
    }


class AsyncHttpClient:
    """Async HTTP client bundling retry, rate limiting and concurrency control.

    A single instance is shared by every component in one run so that the
    connection pool, the concurrency semaphore and the pacing limiter apply
    globally rather than per call site.
    """

    def __init__(self, settings: Settings) -> None:
        """Create a client configured from application settings."""
        self._settings = settings
        self._policy = RetryPolicy(
            max_retries=settings.max_retries,
            backoff=settings.retry_backoff,
            backoff_max=settings.retry_backoff_max,
        )
        self._limiter = RateLimiter(settings.min_request_interval)
        limits = httpx.Limits(
            max_connections=settings.max_concurrency * 2,
            max_keepalive_connections=settings.max_concurrency,
        )
        timeout = httpx.Timeout(
            settings.request_timeout,
            connect=settings.connect_timeout,
        )
        # HTTP/2 needs the `h2` package; fall back transparently when absent.
        try:
            self._client = httpx.AsyncClient(
                headers=build_headers(settings),
                limits=limits,
                timeout=timeout,
                follow_redirects=True,
                verify=settings.verify_ssl,
                http2=settings.http2,
            )
        except ImportError:
            logger.debug("HTTP/2 support unavailable; using HTTP/1.1")
            self._client = httpx.AsyncClient(
                headers=build_headers(settings),
                limits=limits,
                timeout=timeout,
                follow_redirects=True,
                verify=settings.verify_ssl,
            )

    @property
    def retry_policy(self) -> RetryPolicy:
        """Retry policy applied to every request made through this client."""
        return self._policy

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
        description: str | None = None,
    ) -> httpx.Response:
        """GET a URL with retries, raising for non-retryable HTTP errors.

        A rate-limit status is converted to :class:`RateLimitError` carrying the
        server's ``Retry-After`` hint so the retry loop can honour it instead of
        guessing a backoff.
        """
        label = description or url
        headers = {"Accept": accept} if accept else None

        async def attempt() -> httpx.Response:
            await self._limiter.acquire()
            response = await self._client.get(url, params=params, headers=headers)
            if response.status_code in RATE_LIMIT_STATUS:
                error = RateLimitError(
                    f"Rate limited by the server ({response.status_code}) while fetching {label}"
                )
                error.retry_after = parse_retry_after(response)  # type: ignore[attr-defined]
                raise error
            response.raise_for_status()
            return response

        return await retry_async(
            attempt,
            self._policy,
            label,
            retry_after_hint=lambda exc: getattr(exc, "retry_after", None),
        )

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> Any:
        """GET a URL and decode the response body as JSON.

        ArcGIS REST answers with HTTP 200 even for service-level errors, so the
        decoded payload is inspected for an ``error`` member as well.
        """
        response = await self.get(
            url, params=params, accept="application/json,text/plain,*/*", description=description
        )
        try:
            payload = response.json()
        except ValueError as exc:
            snippet = response.text[:200]
            raise httpx.HTTPStatusError(
                f"Expected JSON from {url} but received: {snippet!r}",
                request=response.request,
                response=response,
            ) from exc

        if isinstance(payload, dict) and "error" in payload:
            error = payload["error"]
            message = error.get("message", "unknown error") if isinstance(error, dict) else error
            raise httpx.HTTPStatusError(
                f"ArcGIS service error from {url}: {message}",
                request=response.request,
                response=response,
            )
        return payload

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncHttpClient":
        """Enter an async context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the client when leaving the async context."""
        await self.aclose()
