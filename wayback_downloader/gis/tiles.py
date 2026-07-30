"""XYZ tile addressing and grid planning."""

from __future__ import annotations

import math

from wayback_downloader.exceptions import ValidationError
from wayback_downloader.gis.projection import (
    ground_resolution,
    lonlat_to_pixel,
    map_size_px,
    pixel_to_lonlat,
)
from wayback_downloader.models import BoundingBox, Coordinate, TileGrid, TileIndex

# Refuse to plan a download larger than this. At 256 px tiles that is a
# 16384x16384 mosaic, far beyond any reasonable single request.
MAX_TILES_PER_REQUEST = 4096


def tile_for_coordinate(coordinate: Coordinate, zoom: int, tile_size: int = 256) -> TileIndex:
    """Return the tile containing a geographic point."""
    px, py = lonlat_to_pixel(coordinate.longitude, coordinate.latitude, zoom, tile_size)
    span = 1 << zoom
    x = min(span - 1, max(0, int(px // tile_size)))
    y = min(span - 1, max(0, int(py // tile_size)))
    return TileIndex(z=zoom, x=x, y=y)


def plan_grid(
    coordinate: Coordinate,
    zoom: int,
    width: int,
    height: int,
    tile_size: int = 256,
) -> TileGrid:
    """Plan the tile block needed to render a pixel window centred on a point.

    The window is centred exactly on ``coordinate``: the returned ``crop_box``
    places the point at the geometric centre of the output image, which is what
    makes the centre pixel correspond to the coordinate the user typed.
    """
    if width <= 0 or height <= 0:
        raise ValidationError("Image dimensions must be positive.")

    center_px, center_py = lonlat_to_pixel(
        coordinate.longitude, coordinate.latitude, zoom, tile_size
    )

    left = center_px - width / 2.0
    top = center_py - height / 2.0

    world_px = map_size_px(zoom, tile_size)
    # Keep the window inside the world vertically; horizontally it may wrap.
    top = max(0.0, min(top, world_px - height)) if height <= world_px else 0.0

    left_i = int(math.floor(left))
    top_i = int(math.floor(top))

    min_x = int(math.floor(left_i / tile_size))
    max_x = int(math.floor((left_i + width - 1) / tile_size))
    min_y = int(math.floor(top_i / tile_size))
    max_y = int(math.floor((top_i + height - 1) / tile_size))

    columns = max_x - min_x + 1
    rows = max_y - min_y + 1
    if columns * rows > MAX_TILES_PER_REQUEST:
        raise ValidationError(
            f"This request needs {columns * rows} tiles, above the {MAX_TILES_PER_REQUEST} limit. "
            "Reduce --size or --zoom."
        )

    crop_left = left_i - min_x * tile_size
    crop_top = top_i - min_y * tile_size

    return TileGrid(
        z=zoom,
        min_x=min_x,
        min_y=min_y,
        max_x=max_x,
        max_y=max_y,
        tile_size=tile_size,
        crop_box=(crop_left, crop_top, crop_left + width, crop_top + height),
    )


def plan_grid_for_bbox(bbox: BoundingBox, zoom: int, tile_size: int = 256) -> TileGrid:
    """Plan the tile block covering a bounding box at a zoom level."""
    left_px, top_px = lonlat_to_pixel(bbox.west, bbox.north, zoom, tile_size)
    right_px, bottom_px = lonlat_to_pixel(bbox.east, bbox.south, zoom, tile_size)

    width = max(1, int(round(right_px - left_px)))
    height = max(1, int(round(bottom_px - top_px)))

    return plan_grid(bbox.center, zoom, width, height, tile_size)


def grid_bounds(grid: TileGrid) -> BoundingBox:
    """Return the WGS84 extent of a grid's cropped output window."""
    crop_left, crop_top, crop_right, crop_bottom = grid.crop_box
    origin_px = grid.min_x * grid.tile_size
    origin_py = grid.min_y * grid.tile_size

    west, north = pixel_to_lonlat(
        origin_px + crop_left, origin_py + crop_top, grid.z, grid.tile_size
    )
    east, south = pixel_to_lonlat(
        origin_px + crop_right, origin_py + crop_bottom, grid.z, grid.tile_size
    )

    # Normalise longitudes that ran past the antimeridian during planning.
    west = ((west + 180.0) % 360.0) - 180.0
    east = ((east + 180.0) % 360.0) - 180.0
    return BoundingBox(
        west=west,
        south=max(-85.05112878, south),
        east=east if east > west else west + 1e-9,
        north=min(85.05112878, north),
    )


def describe_resolution(coordinate: Coordinate, zoom: int, tile_size: int = 256) -> str:
    """Format the ground resolution at a point as a human-readable string."""
    resolution = ground_resolution(coordinate.latitude, zoom, tile_size)
    if resolution < 1.0:
        return f"{resolution * 100:.1f} cm/px"
    if resolution < 1000.0:
        return f"{resolution:.2f} m/px"
    return f"{resolution / 1000:.2f} km/px"


def crosscheck_with_mercantile(coordinate: Coordinate, zoom: int) -> TileIndex | None:
    """Compute the containing tile via ``mercantile`` when it is installed.

    Used by the test suite to validate the in-house tile maths against a
    reference implementation; returns ``None`` if the package is absent.
    """
    try:
        import mercantile
    except ImportError:
        return None
    tile = mercantile.tile(coordinate.longitude, coordinate.latitude, zoom)
    return TileIndex(z=tile.z, x=tile.x, y=tile.y)
