"""Source-imagery metadata lookups.

Every Wayback release publishes a companion ``MapServer`` describing the
imagery it was built from. Its ``identify`` operation answers "what imagery
covers this point" with the acquisition date, the sensor, the provider and the
native resolution.

The service splits that information across one feature layer per resolution
band (1.9 cm through 2.4 m). Each returned record carries ``MinMapLevel`` and
``MaxMapLevel``, so the record that actually renders at the requested zoom is
the one whose level range contains it -- picking the first result instead would
often report a resolution band the user is not looking at.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from wayback_downloader.config import Settings
from wayback_downloader.models import Coordinate, ImageryMetadata, WaybackRelease
from wayback_downloader.utils.cache import CacheStore
from wayback_downloader.utils.http import AsyncHttpClient
from wayback_downloader.utils.logger import get_logger

logger = get_logger(__name__)

_CACHE_NAMESPACE = "metadata"

# Half-width in degrees of the synthetic map extent sent with an identify
# request. Small enough to stay inside one imagery footprint at any zoom.
_EXTENT_PADDING_DEG = 0.0005


def _to_float(value: Any) -> float | None:
    """Coerce a service attribute to a float, tolerating nulls and blanks."""
    if value in (None, "", "Null", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    """Coerce a service attribute to an int, tolerating nulls and blanks."""
    parsed = _to_float(value)
    return int(parsed) if parsed is not None else None


def _to_date(value: Any) -> dt.date | None:
    """Parse the service's compact ``YYYYMMDD`` acquisition date.

    Also accepts an ISO date, so a record that ever switches format keeps
    working. Anything unrecognisable becomes ``None`` rather than raising --
    metadata is advisory and must never fail a download.
    """
    text = _clean(value)
    if text is None:
        return None
    if len(text) == 8 and text.isdigit():
        try:
            return dt.date(int(text[:4]), int(text[4:6]), int(text[6:]))
        except ValueError:
            return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _clean(value: Any) -> str | None:
    """Normalise a string attribute, mapping the service's null sentinels to None."""
    if value in (None, "", "Null", "null"):
        return None
    return str(value).strip() or None


def select_best_record(results: list[dict[str, Any]], zoom: int) -> dict[str, Any] | None:
    """Choose the identify result whose zoom range covers the requested zoom.

    Falls back to the record with the closest level range, and finally to the
    first result, so a metadata service that omits the level fields still
    produces an answer.
    """
    if not results:
        return None

    scored: list[tuple[int, dict[str, Any]]] = []
    for result in results:
        attributes = result.get("attributes") or {}
        low = _to_int(attributes.get("MinMapLevel"))
        high = _to_int(attributes.get("MaxMapLevel"))
        if low is None or high is None:
            scored.append((10_000, result))
        elif low <= zoom <= high:
            scored.append((0, result))
        else:
            scored.append((min(abs(zoom - low), abs(zoom - high)), result))

    scored.sort(key=lambda item: item[0])
    return scored[0][1]


class MetadataClient:
    """Queries the per-release imagery metadata service."""

    def __init__(self, http: AsyncHttpClient, settings: Settings, cache: CacheStore) -> None:
        """Wire the metadata client to its HTTP, settings and cache collaborators."""
        self._http = http
        self._settings = settings
        self._cache = cache

    async def fetch(
        self, release: WaybackRelease, coordinate: Coordinate, zoom: int
    ) -> ImageryMetadata:
        """Return the source-imagery attributes at a point for one release.

        Metadata is advisory, so any failure degrades to an empty record rather
        than aborting a download that would otherwise succeed.
        """
        results = await self._identify(release, coordinate)
        record = select_best_record(results, zoom)
        if record is None:
            return ImageryMetadata()

        attributes = record.get("attributes") or {}
        return ImageryMetadata(
            provider=_clean(attributes.get("NICE_DESC")),
            product=_clean(attributes.get("NICE_NAME")),
            sensor=_clean(attributes.get("SRC_DESC")),
            acquisition_date=_to_date(attributes.get("SRC_DATE")),
            source_resolution_m=_to_float(attributes.get("SRC_RES")),
            sampled_resolution_m=_to_float(attributes.get("SAMP_RES")),
            accuracy_m=_to_float(attributes.get("SRC_ACC")),
            min_zoom=_to_int(attributes.get("MinMapLevel")),
            max_zoom=_to_int(attributes.get("MaxMapLevel")),
        )

    async def zoom_range(
        self, release: WaybackRelease, coordinate: Coordinate
    ) -> tuple[int, int] | None:
        """Return the span of zoom levels that carry imagery at a point.

        Derived from the ``MinMapLevel``/``MaxMapLevel`` of every overlapping
        resolution band, so it reflects what the service actually publishes here
        rather than a guess. Returns ``None`` when the metadata service is
        unavailable or reports no level information.
        """
        results = await self._identify(release, coordinate)

        lows: list[int] = []
        highs: list[int] = []
        for result in results:
            attributes = result.get("attributes") or {}
            low = _to_int(attributes.get("MinMapLevel"))
            high = _to_int(attributes.get("MaxMapLevel"))
            if low is not None and high is not None and low <= high:
                lows.append(low)
                highs.append(high)

        if not lows:
            return None
        return min(lows), max(highs)

    async def _identify(
        self, release: WaybackRelease, coordinate: Coordinate
    ) -> list[dict[str, Any]]:
        """Run an identify request at a point, returning every overlapping record.

        The request itself does not depend on zoom -- only the choice of which
        returned record to use does -- so results are cached per location and
        reused across zoom levels.
        """
        if not release.metadata_url:
            return []

        cache_key = CacheStore.make_key(
            _CACHE_NAMESPACE,
            release.metadata_url,
            round(coordinate.latitude, 5),
            round(coordinate.longitude, 5),
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        padding = _EXTENT_PADDING_DEG
        params = {
            "f": "json",
            "geometry": json.dumps(
                {
                    "x": coordinate.longitude,
                    "y": coordinate.latitude,
                    "spatialReference": {"wkid": 4326},
                }
            ),
            "geometryType": "esriGeometryPoint",
            "sr": 4326,
            "layers": "all",
            "tolerance": 2,
            "mapExtent": (
                f"{coordinate.longitude - padding},{coordinate.latitude - padding},"
                f"{coordinate.longitude + padding},{coordinate.latitude + padding}"
            ),
            "imageDisplay": "256,256,96",
            "returnGeometry": "false",
        }

        try:
            payload = await self._http.get_json(
                f"{release.metadata_url}/identify",
                params=params,
                description=f"imagery metadata {release.release_date}",
            )
        except Exception as exc:
            logger.debug("Metadata lookup failed for release %s: %s", release.release_num, exc)
            return []

        results = payload.get("results") or []
        self._cache.set(cache_key, results, ttl=self._settings.metadata_cache_ttl)
        return results
