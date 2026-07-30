"""Tests for release matching and local change detection."""

from __future__ import annotations

import datetime as dt

import pytest

from wayback_downloader.api.wayback import (
    WaybackCatalog,
    filter_by_date_range,
    match_nearest_release,
)
from wayback_downloader.config import Settings
from wayback_downloader.exceptions import ImageryUnavailableError
from wayback_downloader.models import Coordinate, ServiceEndpoints, TilemapProbe, WaybackRelease
from wayback_downloader.utils.cache import CacheStore

BASE = "https://wayback.example/MapServer"


def make_release(release_num: int, date: str) -> WaybackRelease:
    """Build a release with a given number and date."""
    return WaybackRelease(
        release_num=release_num,
        item_id=f"item-{release_num}",
        title=f"World Imagery (Wayback {date})",
        tile_url_template=f"{BASE}/tile/{release_num}/{{level}}/{{row}}/{{col}}",
        release_date=dt.date.fromisoformat(date),
    )


RELEASES = [
    make_release(500, "2023-01-10"),
    make_release(400, "2021-06-01"),
    make_release(300, "2021-04-20"),
    make_release(200, "2019-11-05"),
]


def test_matches_the_nearest_release() -> None:
    """The release closest in time to the request is selected."""
    assert match_nearest_release(RELEASES, dt.date(2021, 5, 14)).release_date == dt.date(2021, 6, 1)
    assert match_nearest_release(RELEASES, dt.date(2021, 4, 25)).release_date == dt.date(
        2021, 4, 20
    )


def test_exact_date_matches_itself() -> None:
    """A request on a release date resolves to that release."""
    assert match_nearest_release(RELEASES, dt.date(2019, 11, 5)).release_num == 200


def test_far_future_request_falls_back_to_the_newest() -> None:
    """A date past every release resolves to the most recent one."""
    assert match_nearest_release(RELEASES, dt.date(2026, 1, 1)).release_num == 500


def test_tie_breaks_towards_the_earlier_release() -> None:
    """A date exactly between two releases picks the imagery that already existed."""
    pair = [make_release(1, "2021-01-01"), make_release(2, "2021-01-11")]
    assert match_nearest_release(pair, dt.date(2021, 1, 6)).release_date == dt.date(2021, 1, 1)


def test_matching_an_empty_catalog_raises() -> None:
    """Matching against no releases is an explicit error."""
    with pytest.raises(ImageryUnavailableError):
        match_nearest_release([], dt.date(2021, 1, 1))


def test_date_range_filter() -> None:
    """Range filtering is inclusive and returns newest first."""
    selected = filter_by_date_range(RELEASES, dt.date(2021, 1, 1), dt.date(2021, 12, 31))
    assert [release.release_num for release in selected] == [400, 300]


def test_empty_date_range_raises() -> None:
    """A range containing no releases reports that clearly."""
    with pytest.raises(ImageryUnavailableError, match="No Wayback release"):
        filter_by_date_range(RELEASES, dt.date(2015, 1, 1), dt.date(2015, 12, 31))


class _StubCatalog(WaybackCatalog):
    """A catalog whose network calls are replaced by canned tilemap sizes."""

    def __init__(self, sizes: dict[int, int | None]) -> None:
        """Seed the stub with a byte size per release number."""
        settings = Settings()
        cache = CacheStore(settings.cache_dir, enabled=False)
        super().__init__(http=None, settings=settings, cache=cache)  # type: ignore[arg-type]
        self._sizes = sizes
        self._endpoints = ServiceEndpoints(
            config_url="stub",
            tile_service_base=BASE,
            release_count=len(RELEASES),
            discovered_at=dt.datetime.now(dt.timezone.utc),
        )
        self._releases = list(RELEASES)

    async def load(self):
        """Return the seeded endpoints and releases without any network access."""
        return self._endpoints, self._releases

    async def probe_all(self, releases, tile, on_progress=None):
        """Return the canned probe results."""
        return {
            release.release_num: TilemapProbe(
                release_num=release.release_num,
                valid=self._sizes.get(release.release_num) is not None,
                byte_size=self._sizes.get(release.release_num),
            )
            for release in releases
        }


CESME = Coordinate(latitude=38.7992, longitude=26.9723)


async def test_identical_tiles_collapse_to_one_version() -> None:
    """Releases serving a byte-identical tile are reported once."""
    catalog = _StubCatalog({500: 28054, 400: 28054, 300: 28054, 200: 28054})
    changed = await catalog.releases_with_local_changes(CESME, 14)
    assert [release.release_num for release in changed] == [200]


async def test_each_size_change_starts_a_new_version() -> None:
    """A byte-size change marks a release where the imagery was updated."""
    catalog = _StubCatalog({500: 30000, 400: 28054, 300: 28054, 200: 26202})
    changed = await catalog.releases_with_local_changes(CESME, 14)
    # Newest first: 2023 (30000), 2021-04 (28054), 2019 (26202).
    assert [release.release_num for release in changed] == [500, 300, 200]


async def test_releases_without_imagery_are_skipped() -> None:
    """Releases whose tilemap reports no tile drop out of the version list."""
    catalog = _StubCatalog({500: None, 400: 28054, 300: None, 200: 26202})
    changed = await catalog.releases_with_local_changes(CESME, 14)
    assert [release.release_num for release in changed] == [400, 200]


async def test_no_imagery_anywhere_raises() -> None:
    """A location with no imagery at all produces an actionable error."""
    catalog = _StubCatalog({500: None, 400: None, 300: None, 200: None})
    with pytest.raises(ImageryUnavailableError, match="lower zoom"):
        await catalog.releases_with_local_changes(CESME, 14)
