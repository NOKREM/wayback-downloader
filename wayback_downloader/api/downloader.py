"""Concurrent tile retrieval.

Tiles are fetched with a bounded worker pool over a shared connection pool,
each request carrying the retry and rate-limit policy from
:mod:`wayback_downloader.utils.http`. Successful tiles are cached on disk, so a
repeated or overlapping request costs no network traffic.

A few missing tiles are tolerated and left blank in the mosaic; beyond
:data:`MAX_MISSING_RATIO` the result would be misleading rather than merely
imperfect, so the download fails instead.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from wayback_downloader.config import Settings
from wayback_downloader.exceptions import TileDownloadError
from wayback_downloader.models import TileGrid, TileIndex, WaybackRelease
from wayback_downloader.utils.cache import CacheStore
from wayback_downloader.utils.http import AsyncHttpClient
from wayback_downloader.utils.logger import get_logger

logger = get_logger(__name__)

_CACHE_NAMESPACE = "tile"

# Fraction of a grid that may fail before the whole download is rejected.
MAX_MISSING_RATIO = 0.15

ProgressHook = Callable[[int], None]


@dataclass
class DownloadStats:
    """Counters describing how one grid download went."""

    requested: int = 0
    from_cache: int = 0
    downloaded: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def missing_ratio(self) -> float:
        """Fraction of requested tiles that could not be obtained."""
        return self.failed / self.requested if self.requested else 0.0

    def summary(self) -> str:
        """Render a one-line human-readable summary."""
        return (
            f"{self.downloaded} downloaded, {self.from_cache} cached, "
            f"{self.failed} failed, {self.bytes_downloaded / 1024:.0f} KiB transferred"
        )


class TileDownloader:
    """Fetches all tiles for a grid from one Wayback release."""

    def __init__(self, http: AsyncHttpClient, settings: Settings, cache: CacheStore) -> None:
        """Wire the downloader to its HTTP, settings and cache collaborators."""
        self._http = http
        self._settings = settings
        self._cache = cache

    async def _fetch_tile(self, release: WaybackRelease, tile: TileIndex) -> tuple[bytes, bool]:
        """Fetch one tile, returning its bytes and whether it came from cache."""
        cache_key = CacheStore.make_key(_CACHE_NAMESPACE, release.release_num, str(tile))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached, True

        response = await self._http.get(
            release.tile_url(tile),
            accept="image/avif,image/webp,image/jpeg,image/png,*/*",
            description=f"tile {tile} @ {release.release_date}",
        )
        payload = response.content
        if not payload:
            raise TileDownloadError(f"Tile {tile} came back empty")
        self._cache.set(cache_key, payload, ttl=self._settings.tile_cache_ttl)
        return payload, False

    async def download_grid(
        self,
        release: WaybackRelease,
        grid: TileGrid,
        on_progress: ProgressHook | None = None,
    ) -> tuple[dict[tuple[int, int], bytes], DownloadStats]:
        """Download every tile in a grid, returning them keyed by paste offset.

        Raises :class:`TileDownloadError` when too large a share of the grid is
        missing to produce a trustworthy image.
        """
        placements = grid.placements()
        stats = DownloadStats(requested=len(placements))
        results: dict[tuple[int, int], bytes] = {}
        semaphore = asyncio.Semaphore(self._settings.max_concurrency)
        lock = asyncio.Lock()

        async def worker(offset: tuple[int, int], tile: TileIndex) -> None:
            payload: bytes | None = None
            error: str | None = None
            cache_hit = False
            try:
                async with semaphore:
                    payload, cache_hit = await self._fetch_tile(release, tile)
            except Exception as exc:
                error = f"{tile}: {type(exc).__name__}: {exc}"

            async with lock:
                if payload is not None:
                    results[offset] = payload
                    if cache_hit:
                        stats.from_cache += 1
                    else:
                        stats.downloaded += 1
                        stats.bytes_downloaded += len(payload)
                else:
                    stats.failed += 1
                    if error and len(stats.failures) < 10:
                        stats.failures.append(error)
                if on_progress is not None:
                    on_progress(1)

        await asyncio.gather(
            *(
                worker((placement.offset_x, placement.offset_y), placement.index)
                for placement in placements
            )
        )

        if stats.failed:
            logger.warning("%d of %d tiles failed", stats.failed, stats.requested)
        if stats.missing_ratio > MAX_MISSING_RATIO:
            detail = "\n  ".join(stats.failures) or "no detail captured"
            raise TileDownloadError(
                f"{stats.failed} of {stats.requested} tiles could not be downloaded "
                f"({stats.missing_ratio:.0%} of the image). First failures:\n  {detail}"
            )

        logger.debug("Grid download complete: %s", stats.summary())
        return results, stats
