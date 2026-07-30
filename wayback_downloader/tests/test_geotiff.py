"""Tests for georeferenced raster output.

The assertions that matter -- that the written extent matches the requested one
-- run on both code paths, so the world-file fallback is held to the same
standard as the rasterio path.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from PIL import Image

from wayback_downloader.export.geotiff import _write_worldfile_tiff, write_geotiff
from wayback_downloader.gis.projection import (
    ground_resolution,
    lonlat_to_meters,
    meters_to_lonlat,
)
from wayback_downloader.gis.tiles import plan_grid_for_bbox
from wayback_downloader.models import BoundingBox

BOX = BoundingBox(west=26.965, south=38.795, east=26.980, north=38.805)


def has_rasterio() -> bool:
    """Report whether rasterio is importable in this environment."""
    try:
        import rasterio  # noqa: F401
    except ImportError:
        return False
    return True


def make_image(width: int = 128, height: int = 96) -> Image.Image:
    """Build a small gradient image to write out."""
    image = Image.new("RGB", (width, height))
    image.putdata([(x * 2 % 256, y * 2 % 256, 128) for y in range(height) for x in range(width)])
    return image


def test_writes_a_readable_raster(tmp_path: Path) -> None:
    """The written file reopens at the expected size regardless of backend."""
    image = make_image()
    path = write_geotiff(image, BOX, tmp_path / "out.tif")
    assert path.exists()
    with Image.open(path) as reopened:
        assert reopened.size == image.size


@pytest.mark.skipif(not has_rasterio(), reason="rasterio is not installed")
def test_geotiff_carries_crs_and_transform(tmp_path: Path) -> None:
    """A real GeoTIFF embeds EPSG:3857 and a transform matching the bbox."""
    import rasterio
    from rasterio.warp import transform as warp_transform

    image = make_image()
    path = write_geotiff(image, BOX, tmp_path / "out.tif")

    with rasterio.open(path) as dataset:
        assert dataset.driver == "GTiff"
        assert dataset.crs.to_epsg() == 3857
        assert dataset.count == 3
        assert (dataset.width, dataset.height) == image.size

        left, bottom, right, top = dataset.bounds
        xs, ys = warp_transform(dataset.crs, "EPSG:4326", [left, right], [bottom, top])

    assert xs[0] == pytest.approx(BOX.west, abs=1e-6)
    assert xs[1] == pytest.approx(BOX.east, abs=1e-6)
    assert ys[0] == pytest.approx(BOX.south, abs=1e-6)
    assert ys[1] == pytest.approx(BOX.north, abs=1e-6)


@pytest.mark.skipif(not has_rasterio(), reason="rasterio is not installed")
def test_geotiff_pixels_survive_the_roundtrip(tmp_path: Path) -> None:
    """Written pixels are byte-identical to the source image."""
    import numpy as np
    import rasterio

    image = make_image()
    path = write_geotiff(image, BOX, tmp_path / "out.tif")

    with rasterio.open(path) as dataset:
        written = dataset.read()

    assert np.array_equal(written, np.asarray(image).transpose(2, 0, 1))


def test_worldfile_fallback_georeferences_correctly(tmp_path: Path) -> None:
    """The fallback path writes sidecars describing the same extent."""
    image = make_image()
    path = tmp_path / "out.tif"
    left, top = lonlat_to_meters(BOX.west, BOX.north)
    right, bottom = lonlat_to_meters(BOX.east, BOX.south)
    x_res = (right - left) / image.width
    y_res = (top - bottom) / image.height

    _write_worldfile_tiff(image, path, left, top, x_res, y_res)

    tfw = [float(value) for value in path.with_suffix(".tfw").read_text().split()]
    assert len(tfw) == 6
    assert tfw[1] == 0.0 and tfw[2] == 0.0  # no rotation
    assert tfw[3] < 0  # north-up rasters have a negative y scale

    # World-file coordinates refer to the centre of the top-left pixel.
    origin_x = tfw[4] - tfw[0] / 2
    origin_y = tfw[5] - tfw[3] / 2
    west, north = meters_to_lonlat(origin_x, origin_y)
    assert west == pytest.approx(BOX.west, abs=1e-6)
    assert north == pytest.approx(BOX.north, abs=1e-6)

    assert 'AUTHORITY["EPSG","3857"]' in path.with_suffix(".prj").read_text()


def test_mercator_resolution_relates_to_ground_resolution() -> None:
    """The GeoTIFF transform and the JSON sidecar report different resolutions.

    A GeoTIFF's pixel size is in EPSG:3857 metres, which Web Mercator inflates
    by ``1 / cos(latitude)``. The sidecar reports true ground metres. Both are
    correct; this pins the relationship between them so the discrepancy is
    never mistaken for a georeferencing bug.
    """
    grid = plan_grid_for_bbox(BOX, 16)
    width = grid.crop_box[2] - grid.crop_box[0]

    left, _ = lonlat_to_meters(BOX.west, BOX.north)
    right, _ = lonlat_to_meters(BOX.east, BOX.south)
    mercator_res = (right - left) / width

    latitude = BOX.center.latitude
    reported = ground_resolution(latitude, 16)

    assert reported < mercator_res
    assert reported == pytest.approx(mercator_res * math.cos(math.radians(latitude)), rel=1e-3)
