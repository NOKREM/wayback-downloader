"""Tests for defaulting the bounding box to a layer's advertised extent."""

from __future__ import annotations

import pytest

from wayback_downloader.api.ogc import WmsCapabilities, WmsLayer, bounds_of_layers
from wayback_downloader.cli import _fit_size
from wayback_downloader.exceptions import ValidationError
from wayback_downloader.models import BoundingBox


def capabilities(*layers: WmsLayer) -> WmsCapabilities:
    """Build capabilities carrying the given layers."""
    return WmsCapabilities(
        service_url="https://example.org/wms",
        title="Service",
        version="1.3.0",
        layers=list(layers),
    )


WIDE = WmsLayer(
    "wide", "Wide", ("EPSG:4326",), BoundingBox(west=25.8, south=35.9, east=44.7, north=41.9)
)
NARROW = WmsLayer(
    "narrow", "Narrow", ("EPSG:4326",), BoundingBox(west=36.5, south=36.5, east=41.0, north=40.5)
)
UNBOUNDED = WmsLayer("unbounded", "No extent", ("EPSG:4326",), None)


def test_single_layer_uses_its_own_extent() -> None:
    """The advertised extent is what a bare layer name resolves to."""
    box, missing = bounds_of_layers(capabilities(WIDE), "wide")
    assert missing == []
    assert (box.west, box.south, box.east, box.north) == (25.8, 35.9, 44.7, 41.9)


def test_several_layers_give_the_union() -> None:
    """A composite request has to cover every layer in it."""
    box, _ = bounds_of_layers(capabilities(WIDE, NARROW), "wide,narrow")
    assert box.west == pytest.approx(25.8)
    assert box.south == pytest.approx(35.9)
    assert box.east == pytest.approx(44.7)
    assert box.north == pytest.approx(41.9)


def test_layer_without_an_extent_is_named_back() -> None:
    """A layer publishing no extent is reported, not silently skipped."""
    box, missing = bounds_of_layers(capabilities(WIDE, UNBOUNDED), "wide,unbounded")
    assert missing == ["unbounded"]
    assert box.east == pytest.approx(44.7)


def test_no_extents_at_all_asks_for_explicit_bounds() -> None:
    """With nothing to go on the caller is told what to supply."""
    with pytest.raises(ValidationError, match="--west/--south/--east/--north"):
        bounds_of_layers(capabilities(UNBOUNDED), "unbounded")


def test_unknown_layer_still_reports_what_exists() -> None:
    """Resolving bounds does not swallow a wrong layer name."""
    with pytest.raises(ValidationError, match="wide"):
        bounds_of_layers(capabilities(WIDE), "nope")


def test_a_layer_with_no_extent_says_where_the_box_must_come_from() -> None:
    """An unadvertised layer is often downloadable; its extent is not knowable.

    This service serves 12 layers through its tile cache that its WMS
    capabilities never mention -- they return valid images when named, but
    nothing advertises where they are.
    """
    with pytest.raises(ValidationError) as excinfo:
        bounds_of_layers(capabilities(UNBOUNDED), "unbounded")

    message = str(excinfo.value)
    assert "--west/--south/--east/--north" in message
    assert "does not advertise" in message


def test_whitespace_around_layer_names_is_tolerated() -> None:
    """`--layers a, b` is the same request as `--layers a,b`."""
    box, _ = bounds_of_layers(capabilities(WIDE, NARROW), " wide , narrow ")
    assert box.east == pytest.approx(44.7)


WIDE_BOX = BoundingBox(west=25.8, south=35.9, east=44.7, north=41.9)  # ~3.2:1
TALL_BOX = BoundingBox(west=26.0, south=35.0, east=27.0, north=41.0)  # 1:6


def test_square_size_is_fitted_to_a_derived_extent() -> None:
    """A single --size number would otherwise squash the map.

    The box is 18.9 degrees wide by 6.0 tall, and a WMS renders EPSG:4326
    equirectangular, so degrees map linearly onto pixels.
    """
    assert _fit_size("1200", WIDE_BOX, derived=True) == (1200, 381)
    assert _fit_size("600", TALL_BOX, derived=True) == (100, 600)


def test_explicit_size_is_never_overridden() -> None:
    """`--size 800x600` means exactly that."""
    assert _fit_size("800x600", WIDE_BOX, derived=True) == (800, 600)


def test_user_supplied_bounds_leave_the_size_alone() -> None:
    """Fitting only applies when the extent was chosen for the user.

    Someone who typed both a box and a square size meant the square.
    """
    assert _fit_size("1200", WIDE_BOX, derived=False) == (1200, 1200)


def test_fitted_size_keeps_the_longest_edge() -> None:
    """The number given stays the longest side, so it means what it looks like."""
    width, height = _fit_size("1000", WIDE_BOX, derived=True)
    assert max(width, height) == 1000

    width, height = _fit_size("1000", TALL_BOX, derived=True)
    assert max(width, height) == 1000


def test_fitted_size_never_collapses_to_zero() -> None:
    """An extreme aspect ratio must still produce a usable image."""
    sliver = BoundingBox(west=26.0, south=38.0, east=26.0001, north=41.0)
    width, height = _fit_size("500", sliver, derived=True)
    assert width >= 1 and height >= 1
