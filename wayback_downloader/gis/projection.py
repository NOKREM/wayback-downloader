"""Web Mercator (EPSG:3857) projection maths.

Implemented in pure Python so the core downloader has no compiled geospatial
dependencies. When ``pyproj`` is installed, :func:`crosscheck_with_pyproj` can
verify the implementation against a reference transformer.
"""

from __future__ import annotations

import math

# Semi-major axis of the WGS84 ellipsoid, used as the sphere radius by the
# Web Mercator (EPSG:3857) definition.
EARTH_RADIUS_M = 6_378_137.0
EARTH_CIRCUMFERENCE_M = 2.0 * math.pi * EARTH_RADIUS_M
ORIGIN_SHIFT_M = EARTH_CIRCUMFERENCE_M / 2.0

# Latitude at which the Web Mercator square is truncated.
MAX_LATITUDE = 85.05112877980659


def clamp_latitude(latitude: float) -> float:
    """Clamp a latitude to the Web Mercator valid band."""
    return max(-MAX_LATITUDE, min(MAX_LATITUDE, latitude))


def lonlat_to_meters(longitude: float, latitude: float) -> tuple[float, float]:
    """Project WGS84 degrees to EPSG:3857 metres."""
    x = math.radians(longitude) * EARTH_RADIUS_M
    y = math.log(math.tan(math.pi / 4.0 + math.radians(clamp_latitude(latitude)) / 2.0))
    return x, y * EARTH_RADIUS_M


def meters_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Unproject EPSG:3857 metres back to WGS84 degrees."""
    longitude = math.degrees(x / EARTH_RADIUS_M)
    latitude = math.degrees(2.0 * math.atan(math.exp(y / EARTH_RADIUS_M)) - math.pi / 2.0)
    return longitude, latitude


def map_size_px(zoom: int, tile_size: int = 256) -> int:
    """Return the width of the whole world in pixels at a zoom level."""
    return tile_size << zoom


def lonlat_to_pixel(
    longitude: float, latitude: float, zoom: int, tile_size: int = 256
) -> tuple[float, float]:
    """Convert WGS84 degrees to fractional global pixel coordinates.

    The origin is the top-left of the world (north-west corner), matching the
    XYZ tile convention used by ArcGIS WMTS ``default028mm`` tiles.
    """
    size = map_size_px(zoom, tile_size)
    px = (longitude + 180.0) / 360.0 * size
    sin_lat = math.sin(math.radians(clamp_latitude(latitude)))
    py = (0.5 - math.log((1.0 + sin_lat) / (1.0 - sin_lat)) / (4.0 * math.pi)) * size
    return px, py


def pixel_to_lonlat(px: float, py: float, zoom: int, tile_size: int = 256) -> tuple[float, float]:
    """Convert fractional global pixel coordinates back to WGS84 degrees."""
    size = map_size_px(zoom, tile_size)
    longitude = px / size * 360.0 - 180.0
    n = math.pi * (1.0 - 2.0 * py / size)
    latitude = math.degrees(math.atan(math.sinh(n)))
    return longitude, latitude


def ground_resolution(latitude: float, zoom: int, tile_size: int = 256) -> float:
    """Return metres per pixel at a given latitude and zoom level.

    Resolution is latitude-dependent because Web Mercator stretches distances
    by ``1 / cos(latitude)``.
    """
    return (
        math.cos(math.radians(clamp_latitude(latitude)))
        * EARTH_CIRCUMFERENCE_M
        / map_size_px(zoom, tile_size)
    )


def map_scale(latitude: float, zoom: int, dpi: float = 96.0, tile_size: int = 256) -> float:
    """Return the map scale denominator for a screen at the given DPI."""
    return ground_resolution(latitude, zoom, tile_size) * dpi / 0.0254


def zoom_for_resolution(target_resolution_m: float, latitude: float, tile_size: int = 256) -> int:
    """Return the smallest zoom level meeting a target ground resolution."""
    for zoom in range(0, 24):
        if ground_resolution(latitude, zoom, tile_size) <= target_resolution_m:
            return zoom
    return 23


def crosscheck_with_pyproj(longitude: float, latitude: float) -> tuple[float, float] | None:
    """Reproject a point with ``pyproj`` for verification, if it is installed.

    Returns ``None`` when ``pyproj`` is unavailable, which is the normal case
    for a core-only install.
    """
    try:
        from pyproj import Transformer
    except ImportError:
        return None
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    x, y = transformer.transform(longitude, latitude)
    return float(x), float(y)
