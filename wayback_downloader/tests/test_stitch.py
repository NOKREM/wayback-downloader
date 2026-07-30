"""Tests for tile stitching, cropping and image output."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from wayback_downloader.exceptions import ExportError
from wayback_downloader.gis.stitch import build_image, crop_to_window, save_image, stitch_tiles
from wayback_downloader.gis.tiles import plan_grid
from wayback_downloader.models import Coordinate

CESME = Coordinate(latitude=38.7992, longitude=26.9723)


def make_tile(color: tuple[int, int, int], size: int = 256) -> bytes:
    """Encode a solid-colour PNG tile."""
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="PNG")
    return buffer.getvalue()


def fill_grid(grid, color: tuple[int, int, int] = (10, 120, 200)) -> dict:
    """Produce a full set of identical tiles for a grid."""
    return {
        (placement.offset_x, placement.offset_y): make_tile(color, grid.tile_size)
        for placement in grid.placements()
    }


def test_mosaic_has_the_full_grid_dimensions() -> None:
    """Stitching produces an image spanning every tile in the grid."""
    grid = plan_grid(CESME, 16, 700, 500)
    mosaic = stitch_tiles(grid, fill_grid(grid))
    assert mosaic.size == grid.mosaic_size


def test_crop_yields_the_requested_size() -> None:
    """The cropped output matches the requested pixel dimensions exactly."""
    grid = plan_grid(CESME, 16, 700, 500)
    image = build_image(grid, fill_grid(grid))
    assert image.size == (700, 500)


def test_missing_tiles_leave_a_transparent_gap() -> None:
    """A dropped tile shows as a transparent hole rather than corrupting the image."""
    grid = plan_grid(CESME, 16, 512, 512)
    tiles = fill_grid(grid)
    tiles.pop(next(iter(tiles)))

    mosaic = stitch_tiles(grid, tiles)
    assert mosaic.mode == "RGBA"
    alphas = mosaic.split()[-1].getextrema()
    assert alphas[0] == 0  # at least one fully transparent pixel
    assert alphas[1] == 255  # and the rest is opaque


def test_undecodable_tile_is_skipped() -> None:
    """Corrupt tile bytes are skipped without raising."""
    grid = plan_grid(CESME, 16, 512, 512)
    tiles = fill_grid(grid)
    tiles[next(iter(tiles))] = b"this is not an image"
    assert stitch_tiles(grid, tiles).size == grid.mosaic_size


def test_odd_sized_tile_is_resampled() -> None:
    """A tile served at the wrong size is resampled to fit the grid."""
    grid = plan_grid(CESME, 16, 512, 512)
    tiles = fill_grid(grid)
    key = next(iter(tiles))
    tiles[key] = make_tile((255, 0, 0), size=128)
    assert stitch_tiles(grid, tiles).size == grid.mosaic_size


def test_saves_png_and_jpg(tmp_path: Path) -> None:
    """Both output formats are written and reopen at the right size."""
    grid = plan_grid(CESME, 16, 300, 200)
    image = build_image(grid, fill_grid(grid))

    png = save_image(image, tmp_path / "out.png", "png")
    jpg = save_image(image, tmp_path / "out.jpg", "jpg")

    assert Image.open(png).size == (300, 200)
    assert Image.open(jpg).size == (300, 200)
    assert Image.open(jpg).mode == "RGB"


def test_unknown_format_is_rejected(tmp_path: Path) -> None:
    """An unsupported extension produces a clear export error."""
    grid = plan_grid(CESME, 16, 64, 64)
    image = build_image(grid, fill_grid(grid))
    with pytest.raises(ExportError, match="png or jpg"):
        save_image(image, tmp_path / "out.bmp", "bmp")


def test_empty_crop_window_is_rejected() -> None:
    """A degenerate crop window is reported rather than silently producing nothing."""
    grid = plan_grid(CESME, 16, 256, 256)
    broken = grid.model_copy(update={"crop_box": (10, 10, 10, 10)})
    with pytest.raises(ExportError, match="empty"):
        crop_to_window(Image.new("RGBA", grid.mosaic_size), broken)
