"""Tests for XYZ tile addressing and grid planning."""

from __future__ import annotations

import pytest

from wayback_downloader.exceptions import ValidationError
from wayback_downloader.gis.projection import lonlat_to_pixel
from wayback_downloader.gis.tiles import (
    crosscheck_with_mercantile,
    describe_resolution,
    grid_bounds,
    plan_grid,
    plan_grid_for_bbox,
    tile_for_coordinate,
)
from wayback_downloader.models import BoundingBox, Coordinate

CESME = Coordinate(latitude=38.7992, longitude=26.9723)


def test_known_tile_address() -> None:
    """A hand-computed tile address is reproduced exactly."""
    # Verified against the live Wayback tilemap service.
    tile = tile_for_coordinate(CESME, 14)
    assert (tile.z, tile.x, tile.y) == (14, 9419, 6273)


def test_zoom_zero_is_a_single_tile() -> None:
    """Every coordinate resolves to tile 0/0/0 at zoom 0."""
    for latitude, longitude in [(0, 0), (80, 179), (-80, -179)]:
        tile = tile_for_coordinate(Coordinate(latitude=latitude, longitude=longitude), 0)
        assert (tile.x, tile.y) == (0, 0)


def test_matches_mercantile_when_available() -> None:
    """Cross-check tile addressing against mercantile if it is installed."""
    reference = crosscheck_with_mercantile(CESME, 17)
    if reference is None:
        pytest.skip("mercantile is not installed")
    assert tile_for_coordinate(CESME, 17) == reference


def test_grid_crop_is_exactly_the_requested_size() -> None:
    """The crop window always has the dimensions the user asked for."""
    grid = plan_grid(CESME, 17, 1024, 768)
    left, top, right, bottom = grid.crop_box
    assert right - left == 1024
    assert bottom - top == 768


def test_grid_covers_the_crop_window() -> None:
    """The tile block is large enough to contain the whole crop window."""
    grid = plan_grid(CESME, 18, 1500, 900)
    mosaic_width, mosaic_height = grid.mosaic_size
    left, top, right, bottom = grid.crop_box
    assert 0 <= left and 0 <= top
    assert right <= mosaic_width and bottom <= mosaic_height


def test_requested_coordinate_lands_at_the_image_centre() -> None:
    """The centre pixel of the output corresponds to the requested point."""
    zoom, width, height = 17, 512, 512
    grid = plan_grid(CESME, zoom, width, height)
    center_px, center_py = lonlat_to_pixel(CESME.longitude, CESME.latitude, zoom)

    left, top, _, _ = grid.crop_box
    absolute_left = grid.min_x * grid.tile_size + left
    absolute_top = grid.min_y * grid.tile_size + top

    assert abs((absolute_left + width / 2) - center_px) <= 1.0
    assert abs((absolute_top + height / 2) - center_py) <= 1.0


def test_placement_count_matches_grid_shape() -> None:
    """Every cell of the tile block yields exactly one placement."""
    grid = plan_grid(CESME, 16, 1024, 1024)
    placements = grid.placements()
    assert len(placements) == grid.columns * grid.rows
    assert len({(p.offset_x, p.offset_y) for p in placements}) == len(placements)


def test_antimeridian_columns_wrap() -> None:
    """A grid crossing the antimeridian wraps its tile columns into range."""
    edge = Coordinate(latitude=0.0, longitude=179.999)
    grid = plan_grid(edge, 8, 1024, 256)
    span = 1 << 8
    assert all(0 <= placement.index.x < span for placement in grid.placements())


def test_oversized_request_is_rejected() -> None:
    """A request needing more tiles than the cap is refused with a clear error."""
    with pytest.raises(ValidationError, match="tiles"):
        plan_grid(CESME, 20, 16384, 16384)


def test_grid_bounds_contain_the_coordinate() -> None:
    """The reported extent actually contains the requested point."""
    grid = plan_grid(CESME, 16, 1024, 1024)
    bounds = grid_bounds(grid)
    assert bounds.west < CESME.longitude < bounds.east
    assert bounds.south < CESME.latitude < bounds.north


def test_bbox_grid_covers_the_box() -> None:
    """A bounding-box grid spans at least the requested extent."""
    box = BoundingBox(west=26.95, south=38.78, east=26.99, north=38.81)
    grid = plan_grid_for_bbox(box, 15)
    bounds = grid_bounds(grid)
    assert bounds.west <= box.west + 1e-3
    assert bounds.east >= box.east - 1e-3


def test_describe_resolution_switches_units() -> None:
    """Resolution is reported in centimetres when below one metre."""
    assert describe_resolution(CESME, 19).endswith("cm/px")
    assert describe_resolution(CESME, 10).endswith("m/px")
