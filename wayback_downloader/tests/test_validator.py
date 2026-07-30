"""Tests for user-input validation."""

from __future__ import annotations

import datetime as dt

import pytest

from wayback_downloader.exceptions import ValidationError
from wayback_downloader.utils.validator import (
    parse_size,
    validate_bbox,
    validate_coordinate,
    validate_date,
    validate_date_range,
    validate_zoom,
)


def test_accepts_a_valid_coordinate() -> None:
    """A normal coordinate passes through unchanged."""
    coordinate = validate_coordinate(38.7992, 26.9723)
    assert coordinate.latitude == 38.7992
    assert coordinate.longitude == 26.9723


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0), (86.0, 0.0)],
)
def test_rejects_out_of_range_coordinates(latitude: float, longitude: float) -> None:
    """Out-of-range and beyond-Mercator coordinates are rejected."""
    with pytest.raises(ValidationError):
        validate_coordinate(latitude, longitude)


def test_parses_supported_date_formats() -> None:
    """Several common date spellings resolve to the same date."""
    expected = dt.date(2021, 5, 14)
    for text in ("2021-05-14", "2021/05/14", "14.05.2021", "20210514"):
        assert validate_date(text) == expected


def test_rejects_unparseable_date() -> None:
    """A nonsense date produces a message naming the expected format."""
    with pytest.raises(ValidationError, match="YYYY-MM-DD"):
        validate_date("May 14th")


def test_rejects_date_outside_the_archive() -> None:
    """Dates before the archive begins or in the future are rejected."""
    with pytest.raises(ValidationError, match="predates"):
        validate_date("2009-01-01")
    future = dt.date.today() + dt.timedelta(days=1)
    with pytest.raises(ValidationError, match="future"):
        validate_date(future.isoformat())


@pytest.mark.parametrize("zoom", [-1, 24, 100])
def test_rejects_out_of_range_zoom(zoom: int) -> None:
    """Zoom levels outside the pyramid are rejected."""
    with pytest.raises(ValidationError):
        validate_zoom(zoom)


def test_parses_size_forms() -> None:
    """Square and rectangular size arguments both parse."""
    assert parse_size("1024") == (1024, 1024)
    assert parse_size("1024x768") == (1024, 768)
    assert parse_size("800 X 600") == (800, 600)
    assert parse_size(512) == (512, 512)


@pytest.mark.parametrize("value", ["0", "-100", "abc", "1024x", "99999"])
def test_rejects_bad_sizes(value: str) -> None:
    """Malformed or oversized dimensions are rejected."""
    with pytest.raises(ValidationError):
        parse_size(value)


def test_rejects_inverted_bbox() -> None:
    """A bounding box with swapped corners is rejected."""
    with pytest.raises(ValidationError):
        validate_bbox(27.0, 38.8, 26.9, 38.7)


def test_rejects_inverted_date_range() -> None:
    """A range whose start follows its end is rejected."""
    with pytest.raises(ValidationError, match="after"):
        validate_date_range(dt.date(2022, 1, 1), dt.date(2021, 1, 1))
