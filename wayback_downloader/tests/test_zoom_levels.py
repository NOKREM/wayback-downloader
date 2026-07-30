"""Tests for multi-zoom selection and zoom-range discovery."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from wayback_downloader.api.metadata import MetadataClient
from wayback_downloader.config import Settings
from wayback_downloader.exceptions import ValidationError
from wayback_downloader.models import Coordinate, WaybackRelease
from wayback_downloader.utils.cache import CacheStore
from wayback_downloader.utils.validator import parse_zoom_levels

CESME = Coordinate(latitude=38.7992, longitude=26.9723)


def test_parses_a_span() -> None:
    """An inclusive span expands to every level it covers."""
    assert parse_zoom_levels("14-19") == [14, 15, 16, 17, 18, 19]


def test_parses_a_single_level() -> None:
    """A bare integer yields a one-element list."""
    assert parse_zoom_levels("18") == [18]


def test_parses_a_list() -> None:
    """A comma-separated list keeps only the levels named."""
    assert parse_zoom_levels("12,15,18") == [12, 15, 18]


def test_mixes_spans_and_singles() -> None:
    """Spans and single levels can be combined in one argument."""
    assert parse_zoom_levels("10,14-16,19") == [10, 14, 15, 16, 19]


def test_deduplicates_and_sorts() -> None:
    """Overlapping input collapses to a sorted, unique list."""
    assert parse_zoom_levels("18,14-16,15,16") == [14, 15, 16, 18]


def test_all_defers_to_discovery() -> None:
    """The literal 'all' returns None so the caller queries the service."""
    assert parse_zoom_levels("all") is None
    assert parse_zoom_levels("  ALL  ") is None


@pytest.mark.parametrize("value", ["", "  ", "abc", "14-", "-19", "14-x", ","])
def test_rejects_malformed_input(value: str) -> None:
    """Unparseable range arguments are refused with guidance."""
    with pytest.raises(ValidationError):
        parse_zoom_levels(value)


def test_rejects_inverted_span() -> None:
    """A span whose start exceeds its end is refused."""
    with pytest.raises(ValidationError, match="greater than"):
        parse_zoom_levels("19-14")


@pytest.mark.parametrize("value", ["0-30", "24", "-1"])
def test_rejects_levels_outside_the_pyramid(value: str) -> None:
    """Levels beyond the tile pyramid are refused."""
    with pytest.raises(ValidationError):
        parse_zoom_levels(value)


def make_client() -> MetadataClient:
    """Build a metadata client with caching disabled and no HTTP transport."""
    settings = Settings()
    cache = CacheStore(settings.cache_dir, enabled=False)
    return MetadataClient(http=None, settings=settings, cache=cache)  # type: ignore[arg-type]


RELEASE = WaybackRelease(
    release_num=1,
    item_id="x",
    title="World Imagery (Wayback 2023-03-15)",
    tile_url_template="https://example/tile/1/{level}/{row}/{col}",
    metadata_url="https://example/metadata/MapServer",
    release_date=dt.date(2023, 3, 15),
)


def band(low: int, high: int) -> dict:
    """Build an identify result for one resolution band."""
    return {"attributes": {"MinMapLevel": str(low), "MaxMapLevel": str(high)}}


async def test_zoom_range_spans_every_band(monkeypatch) -> None:
    """The range covers the union of all overlapping resolution bands."""
    client = make_client()
    # Mirrors the live service: a 30cm band at 19 plus a Vivid band at 12-18.
    monkeypatch.setattr(
        client, "_identify", lambda *_: _async([band(19, 19), band(12, 18), band(12, 18)])
    )
    assert await client.zoom_range(RELEASE, CESME) == (12, 19)


async def test_zoom_range_ignores_records_without_levels(monkeypatch) -> None:
    """Records missing level information do not widen the range."""
    client = make_client()
    monkeypatch.setattr(client, "_identify", lambda *_: _async([{"attributes": {}}, band(14, 17)]))
    assert await client.zoom_range(RELEASE, CESME) == (14, 17)


async def test_zoom_range_ignores_inverted_records(monkeypatch) -> None:
    """A band whose min exceeds its max is discarded as corrupt."""
    client = make_client()
    monkeypatch.setattr(client, "_identify", lambda *_: _async([band(20, 3), band(15, 18)]))
    assert await client.zoom_range(RELEASE, CESME) == (15, 18)


async def test_zoom_range_is_none_without_metadata(monkeypatch) -> None:
    """No usable records means the caller must be told to pass an explicit span."""
    client = make_client()
    monkeypatch.setattr(client, "_identify", lambda *_: _async([]))
    assert await client.zoom_range(RELEASE, CESME) is None


class _CountingHttp:
    """A stand-in HTTP client that counts identify requests."""

    def __init__(self, results: list[dict]) -> None:
        """Seed the canned identify payload."""
        self.calls = 0
        self._results = results

    async def get_json(self, url: str, **_kwargs: Any) -> dict:
        """Return the canned payload and record that a request was made."""
        self.calls += 1
        return {"results": self._results}


async def test_one_identify_request_serves_every_zoom_level(tmp_path: Path) -> None:
    """A multi-zoom download hits the metadata service exactly once per release.

    The identify request does not depend on zoom -- only the choice of which
    returned record to use does -- so its cache key omits zoom. Without that,
    a 17-level download would issue 17 identical requests per release.
    """
    settings = Settings()
    http = _CountingHttp([band(19, 19), band(12, 18)])
    cache = CacheStore(tmp_path / "cache", enabled=True)
    client = MetadataClient(http=http, settings=settings, cache=cache)  # type: ignore[arg-type]

    try:
        for zoom in range(12, 20):
            await client.fetch(RELEASE, CESME, zoom)
        await client.zoom_range(RELEASE, CESME)
    finally:
        cache.close()

    assert http.calls == 1


async def test_zoom_still_selects_a_different_band_from_one_response(tmp_path: Path) -> None:
    """Sharing the response must not flatten the per-zoom band selection."""
    settings = Settings()
    http = _CountingHttp(
        [
            {"attributes": {"MinMapLevel": "19", "MaxMapLevel": "19", "NICE_NAME": "Metro"}},
            {"attributes": {"MinMapLevel": "12", "MaxMapLevel": "18", "NICE_NAME": "Vivid"}},
        ]
    )
    cache = CacheStore(tmp_path / "cache", enabled=True)
    client = MetadataClient(http=http, settings=settings, cache=cache)  # type: ignore[arg-type]

    try:
        assert (await client.fetch(RELEASE, CESME, 17)).product == "Vivid"
        assert (await client.fetch(RELEASE, CESME, 19)).product == "Metro"
    finally:
        cache.close()

    assert http.calls == 1


async def _async(value):
    """Wrap a plain value in a coroutine."""
    return value
