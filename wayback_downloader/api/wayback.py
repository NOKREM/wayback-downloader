"""The Wayback release catalog and local change detection.

Two facts drive this module.

First, a release number is an opaque identifier and not a clock: release 64776
is from 2023 while 64001 is from 2026. Ordering is therefore always done on the
parsed release date.

Second, all ~195 releases serve a tile at any given location, but most of those
tiles are byte-identical reissues of the previous one. The Wayback web app
solves this with the WMTS ``tilemap`` resource, which reports a tile's byte
size without transferring the image. Walking the releases in chronological
order and keeping only those whose byte size changed yields exactly the dates
on which the imagery at that spot was actually updated -- typically a handful
out of 195, at a cost of one small JSON response each.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Callable, Sequence

from wayback_downloader.api.discovery import EndpointDiscovery
from wayback_downloader.config import Settings
from wayback_downloader.exceptions import ImageryUnavailableError
from wayback_downloader.models import (
    Coordinate,
    ServiceEndpoints,
    TileIndex,
    TilemapProbe,
    WaybackRelease,
)
from wayback_downloader.utils.cache import CacheStore
from wayback_downloader.utils.http import AsyncHttpClient
from wayback_downloader.utils.logger import get_logger

logger = get_logger(__name__)

_PROBE_NAMESPACE = "tilemap"

ProgressHook = Callable[[], None]


class WaybackCatalog:
    """Read access to Wayback releases, both globally and per location."""

    def __init__(self, http: AsyncHttpClient, settings: Settings, cache: CacheStore) -> None:
        """Wire the catalog to its HTTP, settings and cache collaborators."""
        self._http = http
        self._settings = settings
        self._cache = cache
        self._discovery = EndpointDiscovery(http, settings, cache)
        self._endpoints: ServiceEndpoints | None = None
        self._releases: list[WaybackRelease] = []
        self._load_lock = asyncio.Lock()

    async def load(self) -> tuple[ServiceEndpoints, list[WaybackRelease]]:
        """Load the catalog once per instance and return it.

        Concurrent callers share a single discovery round-trip.
        """
        async with self._load_lock:
            if self._endpoints is None:
                self._endpoints, self._releases = await self._discovery.discover()
        return self._endpoints, self._releases

    @property
    def endpoints(self) -> ServiceEndpoints:
        """Return the discovered endpoints, requiring :meth:`load` to have run."""
        if self._endpoints is None:
            raise RuntimeError("WaybackCatalog.load() must be awaited before accessing endpoints")
        return self._endpoints

    async def all_releases(self) -> list[WaybackRelease]:
        """Return every known release, newest first."""
        _, releases = await self.load()
        return list(releases)

    async def probe_release(self, release: WaybackRelease, tile: TileIndex) -> TilemapProbe:
        """Ask the tilemap resource about one release at one tile address.

        A failed probe is reported as "no imagery" rather than raised: an
        unreachable release should drop out of the version list, not abort the
        whole query.
        """
        cache_key = CacheStore.make_key(_PROBE_NAMESPACE, release.release_num, str(tile))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return TilemapProbe(**cached)

        url = self.endpoints.tilemap_url(release.release_num, tile)
        try:
            payload = await self._http.get_json(
                url, description=f"tilemap {release.release_date} {tile}"
            )
        except Exception as exc:
            logger.debug("Tilemap probe failed for release %s: %s", release.release_num, exc)
            return TilemapProbe(release_num=release.release_num, valid=False, byte_size=None)

        sizes = payload.get("size") or []
        probe = TilemapProbe(
            release_num=release.release_num,
            valid=bool(payload.get("valid", False)),
            byte_size=int(sizes[0]) if sizes else None,
        )
        self._cache.set(cache_key, probe.model_dump(), ttl=self._settings.tile_cache_ttl)
        return probe

    async def probe_all(
        self,
        releases: Sequence[WaybackRelease],
        tile: TileIndex,
        on_progress: ProgressHook | None = None,
    ) -> dict[int, TilemapProbe]:
        """Probe many releases concurrently, bounded by the concurrency setting."""
        semaphore = asyncio.Semaphore(self._settings.max_concurrency)

        async def guarded(release: WaybackRelease) -> TilemapProbe:
            async with semaphore:
                probe = await self.probe_release(release, tile)
            if on_progress is not None:
                on_progress()
            return probe

        probes = await asyncio.gather(*(guarded(release) for release in releases))
        return {probe.release_num: probe for probe in probes}

    async def releases_with_local_changes(
        self,
        coordinate: Coordinate,
        zoom: int,
        on_progress: ProgressHook | None = None,
    ) -> list[WaybackRelease]:
        """Return the releases where the imagery at this point actually changed.

        Releases are walked oldest-first and kept when the tile's byte size
        differs from the previously kept one, so the result is the set of dates
        on which new imagery landed at this location. The list comes back
        newest-first to match the rest of the API.
        """
        from wayback_downloader.gis.tiles import tile_for_coordinate

        _, releases = await self.load()
        tile = tile_for_coordinate(coordinate, zoom, self._settings.tile_size)
        logger.debug("Probing %d releases at tile %s", len(releases), tile)

        probes = await self.probe_all(releases, tile, on_progress=on_progress)

        chronological = sorted(releases, key=lambda item: (item.release_date, item.release_num))
        changed: list[WaybackRelease] = []
        previous_size: int | None = None

        for release in chronological:
            probe = probes.get(release.release_num)
            if probe is None or not probe.has_imagery:
                continue
            if probe.byte_size != previous_size:
                changed.append(release)
                previous_size = probe.byte_size

        if not changed:
            raise ImageryUnavailableError(
                f"No Wayback imagery is available at {coordinate} for zoom {zoom}. "
                "Try a lower zoom level."
            )

        logger.info(
            "%d of %d releases contain local changes at %s (zoom %d)",
            len(changed),
            len(releases),
            coordinate,
            zoom,
        )
        changed.reverse()
        return changed

    async def available_releases(
        self,
        coordinate: Coordinate,
        zoom: int,
        only_local_changes: bool = True,
        on_progress: ProgressHook | None = None,
    ) -> list[WaybackRelease]:
        """Return the releases usable at a location, newest first.

        With ``only_local_changes`` disabled every release that serves a tile is
        returned, including byte-identical reissues.
        """
        if only_local_changes:
            return await self.releases_with_local_changes(coordinate, zoom, on_progress)

        from wayback_downloader.gis.tiles import tile_for_coordinate

        _, releases = await self.load()
        tile = tile_for_coordinate(coordinate, zoom, self._settings.tile_size)
        probes = await self.probe_all(releases, tile, on_progress=on_progress)
        usable = [
            release
            for release in releases
            if (probe := probes.get(release.release_num)) is not None and probe.has_imagery
        ]
        if not usable:
            raise ImageryUnavailableError(
                f"No Wayback imagery is available at {coordinate} for zoom {zoom}."
            )
        return usable


def match_nearest_release(releases: Sequence[WaybackRelease], target: dt.date) -> WaybackRelease:
    """Pick the release whose date is closest to ``target``.

    Ties are broken towards the earlier release, so a date exactly between two
    versions resolves to imagery that already existed on that date rather than
    imagery captured afterwards.
    """
    if not releases:
        raise ImageryUnavailableError("No Wayback releases available to match against.")
    return min(
        releases,
        key=lambda release: (abs((release.release_date - target).days), release.release_date),
    )


def filter_by_date_range(
    releases: Sequence[WaybackRelease], start: dt.date, end: dt.date
) -> list[WaybackRelease]:
    """Return the releases falling inside an inclusive date range, newest first."""
    selected = [release for release in releases if start <= release.release_date <= end]
    if not selected:
        raise ImageryUnavailableError(
            f"No Wayback release falls between {start.isoformat()} and {end.isoformat()} "
            "at this location."
        )
    return sorted(selected, key=lambda release: release.release_date, reverse=True)
