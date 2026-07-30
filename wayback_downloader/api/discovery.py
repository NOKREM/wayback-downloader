"""Runtime discovery of the Wayback REST endpoints.

Nothing about the tile service is hard-coded into a request path. The only
fixed input is a bootstrap configuration document; the tile service host, the
service path and every per-release tile URL are read out of that document and
the service base is *derived* from the URL template the service publishes for
itself. If Esri moves the service, renames the WMTS path or changes the
placeholder syntax, discovery adapts without a code change.

Two safeguards make that adaptation observable:

* the parser accepts several spellings of every field, so a renamed key does
  not break the run;
* :func:`detect_schema_drift` reports fields that no longer match what this
  version was written against, so drift surfaces as a warning instead of a
  silent behaviour change.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Iterable

from wayback_downloader.config import CONFIG_URL_CANDIDATES, Settings
from wayback_downloader.exceptions import EndpointDiscoveryError
from wayback_downloader.models import ServiceEndpoints, WaybackRelease
from wayback_downloader.utils.cache import CacheStore
from wayback_downloader.utils.http import AsyncHttpClient
from wayback_downloader.utils.logger import get_logger

logger = get_logger(__name__)

# Accepted spellings for every field, most-specific first. The parser walks
# each list and takes the first key present in the record.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "item_id": ("itemID", "itemId", "item_id", "id"),
    "title": ("itemTitle", "itemtitle", "title", "name"),
    "tile_url": ("itemURL", "itemUrl", "item_url", "url", "tileUrl", "urlTemplate"),
    "metadata_url": ("metadataLayerUrl", "metadataUrl", "metadata_layer_url"),
    "metadata_item_id": ("metadataLayerItemID", "metadataLayerItemId", "metadata_item_id"),
    "layer_identifier": ("layerIdentifier", "layer_identifier", "layerId"),
    "release_num": ("releaseNum", "releaseNumber", "release_num"),
    "release_date": ("releaseDateLabel", "releaseDate", "release_date"),
}

# Placeholder spellings normalised to the {level}/{row}/{col} form used here.
_PLACEHOLDER_ALIASES = {
    "{z}": "{level}",
    "{Z}": "{level}",
    "{zoom}": "{level}",
    "{y}": "{row}",
    "{Y}": "{row}",
    "{x}": "{col}",
    "{X}": "{col}",
}

_TILE_PATH_RE = re.compile(
    r"^(?P<base>.+?)/tile/(?P<release>\d+)/\{level\}/\{row\}/\{col\}/?$",
    re.IGNORECASE,
)

_CACHE_NAMESPACE = "catalog"


def _first_present(record: dict[str, Any], field: str) -> Any:
    """Return the first aliased key present in a config record."""
    for alias in _FIELD_ALIASES[field]:
        if alias in record and record[alias] not in (None, ""):
            return record[alias]
    return None


def _normalize_placeholders(url: str) -> str:
    """Rewrite tile URL placeholders into the canonical level/row/col form."""
    for alias, canonical in _PLACEHOLDER_ALIASES.items():
        url = url.replace(alias, canonical)
    return url


def _iter_records(payload: Any) -> Iterable[tuple[str | None, dict[str, Any]]]:
    """Yield ``(key, record)`` pairs from either config document shape.

    The document is currently an object keyed by release number, but a future
    revision could ship a plain array; both are handled.
    """
    if isinstance(payload, dict):
        for key, record in payload.items():
            if isinstance(record, dict):
                yield str(key), record
    elif isinstance(payload, list):
        for record in payload:
            if isinstance(record, dict):
                yield None, record
    else:
        raise EndpointDiscoveryError(
            f"Wayback config has an unexpected top-level type: {type(payload).__name__}"
        )


def parse_release(key: str | None, record: dict[str, Any]) -> WaybackRelease | None:
    """Build a :class:`WaybackRelease` from one config record.

    Returns ``None`` for records that cannot be interpreted, so a single
    malformed entry never aborts the whole catalog load.
    """
    tile_url = _first_present(record, "tile_url")
    title = _first_present(record, "title")
    if not tile_url or not title:
        return None

    tile_url = _normalize_placeholders(str(tile_url))

    release_num = _first_present(record, "release_num")
    if release_num is None and key is not None and str(key).isdigit():
        release_num = key
    if release_num is None:
        match = _TILE_PATH_RE.match(tile_url)
        release_num = match.group("release") if match else None
    if release_num is None:
        return None

    raw_date = _first_present(record, "release_date")
    try:
        release_date = (
            dt.date.fromisoformat(str(raw_date))
            if raw_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(raw_date))
            else WaybackRelease.parse_release_date(str(title))
        )
    except ValueError:
        logger.debug("Dropping release %s: no parseable date in %r", release_num, title)
        return None

    return WaybackRelease(
        release_num=int(release_num),
        item_id=str(_first_present(record, "item_id") or ""),
        title=str(title),
        tile_url_template=tile_url,
        metadata_url=(
            str(_first_present(record, "metadata_url")).rstrip("/")
            if _first_present(record, "metadata_url")
            else None
        ),
        metadata_item_id=(
            str(_first_present(record, "metadata_item_id"))
            if _first_present(record, "metadata_item_id")
            else None
        ),
        layer_identifier=(
            str(_first_present(record, "layer_identifier"))
            if _first_present(record, "layer_identifier")
            else None
        ),
        release_date=release_date,
    )


def derive_service_base(releases: Iterable[WaybackRelease]) -> str:
    """Derive the shared tile-service base URL from published tile templates.

    The base is whatever precedes ``/tile/{releaseNum}/`` and is taken by
    majority vote across releases, so one odd entry cannot redirect every
    request to the wrong host.
    """
    counts: dict[str, int] = {}
    for release in releases:
        match = _TILE_PATH_RE.match(release.tile_url_template)
        if match:
            base = match.group("base").rstrip("/")
            counts[base] = counts.get(base, 0) + 1

    if not counts:
        raise EndpointDiscoveryError(
            "Could not derive the tile service base URL: no tile template matched the "
            "expected '/tile/{releaseNum}/{level}/{row}/{col}' layout. The Wayback "
            "endpoint schema has likely changed."
        )
    return max(counts.items(), key=lambda item: item[1])[0]


def detect_schema_drift(payload: Any, releases: list[WaybackRelease]) -> list[str]:
    """Report config fields that deviate from the expected schema.

    Drift is informational: the run continues, but the operator learns that the
    remote contract moved and the adapters may need updating.
    """
    warnings: list[str] = []
    total = sum(1 for _ in _iter_records(payload))
    if total and len(releases) < total:
        warnings.append(f"{total - len(releases)} of {total} config records could not be parsed")

    if releases:
        sample = next(iter(_iter_records(payload)))[1]
        for field, aliases in _FIELD_ALIASES.items():
            if field in {"release_num", "release_date"}:
                continue
            if not any(alias in sample for alias in aliases):
                warnings.append(f"config records no longer carry a '{field}' field")

        without_metadata = sum(1 for release in releases if not release.metadata_url)
        if without_metadata == len(releases):
            warnings.append("no release exposes a metadata layer URL")

    for warning in warnings:
        logger.warning("Wayback schema drift: %s", warning)
    return warnings


class EndpointDiscovery:
    """Fetches and interprets the Wayback bootstrap configuration."""

    def __init__(self, http: AsyncHttpClient, settings: Settings, cache: CacheStore) -> None:
        """Wire the discovery client to its HTTP, settings and cache collaborators."""
        self._http = http
        self._settings = settings
        self._cache = cache

    def _candidate_urls(self) -> list[str]:
        """Return bootstrap URLs to try, with any configured override first."""
        urls = [self._settings.config_url]
        urls.extend(url for url in CONFIG_URL_CANDIDATES if url not in urls)
        return urls

    async def _fetch_config(self) -> tuple[str, Any]:
        """Fetch the bootstrap document from the first reachable mirror."""
        cache_key = CacheStore.make_key(_CACHE_NAMESPACE, "raw", self._settings.config_url)
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("Using cached Wayback configuration")
            return cached["url"], cached["payload"]

        failures: list[str] = []
        for url in self._candidate_urls():
            try:
                payload = await self._http.get_json(url, description="Wayback configuration")
            except Exception as exc:
                failures.append(f"{url}: {type(exc).__name__}: {exc}")
                logger.debug("Config mirror failed -- %s", failures[-1])
                continue

            self._cache.set(
                cache_key, {"url": url, "payload": payload}, ttl=self._settings.catalog_cache_ttl
            )
            return url, payload

        raise EndpointDiscoveryError(
            "Could not reach any Wayback configuration endpoint.\n  " + "\n  ".join(failures)
        )

    async def discover(self) -> tuple[ServiceEndpoints, list[WaybackRelease]]:
        """Discover the service endpoints and the full release catalog.

        Releases come back sorted newest-first *by release date*, which is not
        the same as sorting by release number.
        """
        config_url, payload = await self._fetch_config()

        releases: list[WaybackRelease] = []
        for key, record in _iter_records(payload):
            release = parse_release(key, record)
            if release is not None:
                releases.append(release)

        if not releases:
            raise EndpointDiscoveryError(
                f"The Wayback configuration at {config_url} contained no usable releases. "
                "The endpoint schema has likely changed."
            )

        detect_schema_drift(payload, releases)
        releases.sort(key=lambda item: (item.release_date, item.release_num), reverse=True)

        endpoints = ServiceEndpoints(
            config_url=config_url,
            tile_service_base=derive_service_base(releases),
            release_count=len(releases),
            discovered_at=dt.datetime.now(dt.timezone.utc),
        )
        logger.info(
            "Discovered %d Wayback releases (%s .. %s) at %s",
            len(releases),
            releases[-1].release_date.isoformat(),
            releases[0].release_date.isoformat(),
            endpoints.tile_service_base,
        )
        return endpoints, releases

    async def verify_service(self, endpoints: ServiceEndpoints) -> dict[str, Any]:
        """Query the derived service root to confirm the endpoint is live."""
        return await self._http.get_json(
            endpoints.tile_service_base,
            params={"f": "json"},
            description="tile service root",
        )
