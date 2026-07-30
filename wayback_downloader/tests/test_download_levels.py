"""Tests for the multi-zoom orchestration in :meth:`WaybackService.download_levels`."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Sequence

import pytest

from wayback_downloader.api.wayback import filter_by_date_range
from wayback_downloader.exceptions import ImageryUnavailableError
from wayback_downloader.models import Coordinate, DownloadRequest, WaybackRelease
from wayback_downloader.service import WaybackService

CESME = Coordinate(latitude=38.7992, longitude=26.9723)


def make_release(date: str) -> WaybackRelease:
    """Build a release dated as given."""
    return WaybackRelease(
        release_num=int(date.replace("-", "")),
        item_id=f"item-{date}",
        title=f"World Imagery (Wayback {date})",
        tile_url_template="https://example/tile/1/{level}/{row}/{col}",
        release_date=dt.date.fromisoformat(date),
    )


# Deliberately mirrors the live service: the coarse level sees updates the
# detailed levels never do, and the most detailed level sees almost none.
AVAILABILITY: dict[int, list[str]] = {
    15: ["2019-10-09", "2021-05-19", "2022-04-06", "2023-03-15"],
    16: ["2019-10-09", "2020-03-23", "2023-03-15"],
    19: ["2020-03-23"],
}


class _StubService(WaybackService):
    """A service whose network calls are replaced by canned availability."""

    def __init__(self, tmp_path: Path) -> None:
        """Build the stub with caching disabled and output under a temp path."""
        super().__init__(use_cache=False)
        self.settings.output_dir = tmp_path
        self.downloaded: list[tuple[int, str, str]] = []

    async def list_versions(self, coordinate, zoom, only_local_changes=True):
        """Return the canned release list for a zoom level."""
        if zoom not in AVAILABILITY:
            raise ImageryUnavailableError(f"no imagery at zoom {zoom}")
        return [make_release(date) for date in reversed(AVAILABILITY[zoom])]

    # Stubs the two halves of a download separately, mirroring the pipeline in
    # `download_many`: the network half is faked, the CPU half passes through.
    async def _fetch(
        self, release, request, output_dir=None, filename_stem=None, write_geotiff=False
    ):
        """Record the call instead of fetching anything."""
        self.downloaded.append(
            (request.zoom, release.release_date.isoformat(), filename_stem or "")
        )
        return filename_stem

    async def _encode(self, prepared):
        """Pass the recorded stem straight through as the result."""
        return prepared


def make_request(requested_date: dt.date = dt.date(2022, 4, 15)) -> DownloadRequest:
    """Build a small download request for a given target date."""
    return DownloadRequest(
        coordinate=CESME,
        requested_date=requested_date,
        zoom=16,
        width=256,
        height=256,
    )


async def run(
    tmp_path: Path,
    zooms: Sequence[int],
    select=None,
    requested_date: dt.date = dt.date(2022, 4, 15),
):
    """Drive download_levels against the stub and return (grouped, service)."""
    service = _StubService(tmp_path)
    try:
        grouped = await service.download_levels(make_request(requested_date), zooms, select=select)
    finally:
        await service.close()
    return grouped, service


async def test_each_level_resolves_its_own_release(tmp_path: Path) -> None:
    """The nearest release is computed per level, not once and reused."""
    grouped, service = await run(tmp_path, [15, 16])

    picked = {zoom: date for zoom, date, _ in service.downloaded}
    # 2022-04-15 is 9 days from zoom 15's 2022-04-06, but zoom 16 has no such
    # release and must fall back to 2023-03-15.
    assert picked[15] == "2022-04-06"
    assert picked[16] == "2023-03-15"
    assert set(grouped) == {15, 16}


async def test_filenames_carry_the_zoom_suffix(tmp_path: Path) -> None:
    """Two levels landing on the same date produce distinct filenames.

    Asking for 2020-04-01 makes zoom 16 and zoom 19 both resolve to
    2020-03-23 -- without the suffix the second would overwrite the first.
    """
    _, service = await run(tmp_path, [16, 19], requested_date=dt.date(2020, 4, 1))

    assert [date for _, date, _ in service.downloaded] == ["2020-03-23", "2020-03-23"]
    stems = {stem for _, _, stem in service.downloaded}
    assert stems == {"2020-03-23_z16", "2020-03-23_z19"}
    assert len(stems) == len(service.downloaded)


async def test_selector_controls_how_many_releases_per_level(tmp_path: Path) -> None:
    """A range selector downloads every matching release at each level."""
    grouped, service = await run(
        tmp_path,
        [15, 16],
        select=lambda candidates: filter_by_date_range(
            candidates, dt.date(2019, 1, 1), dt.date(2021, 12, 31)
        ),
    )

    assert [date for zoom, date, _ in service.downloaded if zoom == 15] == [
        "2019-10-09",
        "2021-05-19",
    ]
    assert [date for zoom, date, _ in service.downloaded if zoom == 16] == [
        "2019-10-09",
        "2020-03-23",
    ]
    assert len(grouped[15]) == 2


async def test_list_selector_takes_everything(tmp_path: Path) -> None:
    """Passing ``list`` downloads every candidate at each level."""
    grouped, _ = await run(tmp_path, [15], select=list)
    assert len(grouped[15]) == len(AVAILABILITY[15])


async def test_unavailable_level_is_skipped_not_fatal(tmp_path: Path) -> None:
    """A level with no imagery drops out while the others still succeed."""
    grouped, _ = await run(tmp_path, [15, 17, 16])
    assert set(grouped) == {15, 16}  # 17 is absent from AVAILABILITY


async def test_all_levels_failing_raises_with_detail(tmp_path: Path) -> None:
    """When no level works the error names each failure."""
    with pytest.raises(ImageryUnavailableError, match="zoom 17"):
        await run(tmp_path, [17, 18])


async def test_levels_are_deduplicated_and_ordered(tmp_path: Path) -> None:
    """Repeated levels are collapsed and processed in ascending order."""
    grouped, service = await run(tmp_path, [16, 15, 16])
    assert list(grouped) == [15, 16]
    assert [zoom for zoom, _, _ in service.downloaded] == [15, 16]


async def test_empty_selection_skips_the_level(tmp_path: Path) -> None:
    """A selector returning nothing skips that level rather than crashing."""
    with pytest.raises(ImageryUnavailableError):
        await run(tmp_path, [15, 16], select=lambda _candidates: [])
