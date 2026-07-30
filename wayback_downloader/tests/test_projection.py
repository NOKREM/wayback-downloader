"""Tests for the Web Mercator projection maths."""

from __future__ import annotations

import math

import pytest

from wayback_downloader.gis.projection import (
    EARTH_CIRCUMFERENCE_M,
    MAX_LATITUDE,
    clamp_latitude,
    ground_resolution,
    lonlat_to_meters,
    lonlat_to_pixel,
    map_size_px,
    meters_to_lonlat,
    pixel_to_lonlat,
    zoom_for_resolution,
)


def test_origin_maps_to_zero_meters() -> None:
    """The intersection of the equator and prime meridian is the 3857 origin."""
    x, y = lonlat_to_meters(0.0, 0.0)
    assert x == pytest.approx(0.0, abs=1e-9)
    assert y == pytest.approx(0.0, abs=1e-9)


def test_meters_roundtrip() -> None:
    """Projecting and unprojecting returns the original coordinate."""
    for longitude, latitude in [(26.9723, 38.7992), (-122.4, 37.8), (151.2, -33.9)]:
        x, y = lonlat_to_meters(longitude, latitude)
        back_lon, back_lat = meters_to_lonlat(x, y)
        assert back_lon == pytest.approx(longitude, abs=1e-9)
        assert back_lat == pytest.approx(latitude, abs=1e-9)


def test_pixel_roundtrip() -> None:
    """Pixel conversion is lossless within floating point tolerance."""
    for zoom in (0, 8, 14, 19):
        px, py = lonlat_to_pixel(26.9723, 38.7992, zoom)
        longitude, latitude = pixel_to_lonlat(px, py, zoom)
        assert longitude == pytest.approx(26.9723, abs=1e-7)
        assert latitude == pytest.approx(38.7992, abs=1e-7)


def test_world_corners() -> None:
    """The Mercator square spans the full pixel extent at every zoom."""
    for zoom in (0, 3, 10):
        size = map_size_px(zoom)
        assert lonlat_to_pixel(-180.0, MAX_LATITUDE, zoom)[0] == pytest.approx(0.0)
        assert lonlat_to_pixel(-180.0, MAX_LATITUDE, zoom)[1] == pytest.approx(0.0, abs=1e-6)
        assert lonlat_to_pixel(180.0, -MAX_LATITUDE, zoom)[0] == pytest.approx(size)
        assert lonlat_to_pixel(180.0, -MAX_LATITUDE, zoom)[1] == pytest.approx(size, abs=1e-6)


def test_latitude_is_clamped() -> None:
    """Latitudes beyond the Mercator cutoff are clamped rather than diverging."""
    assert clamp_latitude(90.0) == MAX_LATITUDE
    assert clamp_latitude(-90.0) == -MAX_LATITUDE
    assert math.isfinite(lonlat_to_meters(0.0, 90.0)[1])


def test_ground_resolution_at_equator() -> None:
    """At the equator, zoom 0 resolution is the circumference over 256 pixels."""
    assert ground_resolution(0.0, 0) == pytest.approx(EARTH_CIRCUMFERENCE_M / 256)
    # Each zoom level halves the ground distance covered by one pixel.
    assert ground_resolution(0.0, 1) == pytest.approx(ground_resolution(0.0, 0) / 2)


def test_ground_resolution_shrinks_with_latitude() -> None:
    """Web Mercator compresses ground distance per pixel away from the equator."""
    assert ground_resolution(60.0, 15) < ground_resolution(0.0, 15)


def test_zoom_for_resolution() -> None:
    """The chosen zoom actually meets the requested ground resolution."""
    zoom = zoom_for_resolution(0.5, 38.8)
    assert ground_resolution(38.8, zoom) <= 0.5
    assert ground_resolution(38.8, zoom - 1) > 0.5


def test_matches_pyproj_when_available() -> None:
    """Cross-check the in-house transform against pyproj if it is installed."""
    from wayback_downloader.gis.projection import crosscheck_with_pyproj

    reference = crosscheck_with_pyproj(26.9723, 38.7992)
    if reference is None:
        pytest.skip("pyproj is not installed")
    x, y = lonlat_to_meters(26.9723, 38.7992)
    assert x == pytest.approx(reference[0], abs=1e-6)
    assert y == pytest.approx(reference[1], abs=1e-6)
