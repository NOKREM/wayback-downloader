"""Tests for metadata record selection and batch input parsing."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from wayback_downloader.api.metadata import select_best_record
from wayback_downloader.exceptions import ValidationError
from wayback_downloader.models import ImageryMetadata
from wayback_downloader.utils.inputs import read_batch, read_csv, read_geojson


def record(layer_id: int, low: int, high: int, name: str) -> dict:
    """Build a synthetic identify result for a resolution band."""
    return {
        "layerId": layer_id,
        "attributes": {"MinMapLevel": str(low), "MaxMapLevel": str(high), "NICE_NAME": name},
    }


def test_picks_the_band_covering_the_zoom() -> None:
    """The record whose level range contains the zoom wins."""
    results = [record(4, 19, 19, "Metro"), record(5, 12, 18, "Vivid")]
    assert select_best_record(results, 17)["attributes"]["NICE_NAME"] == "Vivid"
    assert select_best_record(results, 19)["attributes"]["NICE_NAME"] == "Metro"


def test_falls_back_to_the_closest_band() -> None:
    """With no exact match the nearest level range is used."""
    results = [record(4, 20, 22, "High"), record(5, 5, 8, "Low")]
    assert select_best_record(results, 10)["attributes"]["NICE_NAME"] == "Low"


def test_no_results_yields_none() -> None:
    """An empty identify response selects nothing."""
    assert select_best_record([], 17) is None


def test_compact_acquisition_date_is_parsed() -> None:
    """The service's YYYYMMDD acquisition date is converted to a real date."""
    metadata = ImageryMetadata(acquisition_date="20241029")
    assert metadata.acquisition_date == dt.date(2024, 10, 29)


def test_csv_with_standard_headers(tmp_path: Path) -> None:
    """A plain lat/lon CSV parses into batch entries."""
    path = tmp_path / "points.csv"
    path.write_text(
        "name,lat,lon,date\nCesme,38.7992,26.9723,2022-04-15\nIzmir,38.4237,27.1428,2021-05-14\n",
        encoding="utf-8",
    )
    entries = read_csv(path)
    assert [entry.name for entry in entries] == ["Cesme", "Izmir"]
    assert entries[0].coordinate.latitude == 38.7992
    assert entries[1].date == dt.date(2021, 5, 14)


def test_csv_with_alternative_headers(tmp_path: Path) -> None:
    """Alternative column names, including Turkish, are recognised."""
    path = tmp_path / "points.csv"
    path.write_text("isim;enlem;boylam\nNokta;38.7992;26.9723\n", encoding="utf-8")
    entries = read_csv(path)
    assert entries[0].name == "Nokta"
    assert entries[0].coordinate.longitude == 26.9723


def test_csv_without_coordinates_is_rejected(tmp_path: Path) -> None:
    """A CSV lacking coordinate columns names what it expected to find."""
    path = tmp_path / "bad.csv"
    path.write_text("name,value\nfoo,1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="latitude/longitude"):
        read_csv(path)


def test_geojson_feature_collection(tmp_path: Path) -> None:
    """Point features parse, with properties supplying the name."""
    path = tmp_path / "points.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [26.9723, 38.7992]},
                        "properties": {"name": "Cesme", "zoom": 18},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    entries = read_geojson(path)
    assert entries[0].name == "Cesme"
    assert entries[0].zoom == 18
    assert entries[0].coordinate.latitude == 38.7992


def test_geojson_polygon_uses_its_first_vertex(tmp_path: Path) -> None:
    """A non-point geometry is anchored at its first coordinate pair."""
    path = tmp_path / "poly.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[26.97, 38.79], [26.98, 38.79], [26.98, 38.80]]],
                },
                "properties": {},
            }
        ),
        encoding="utf-8",
    )
    entries = read_geojson(path)
    assert entries[0].coordinate.longitude == pytest.approx(26.97)


def test_unknown_extension_is_rejected(tmp_path: Path) -> None:
    """An unsupported batch file type is refused by name."""
    path = tmp_path / "points.xlsx"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError, match="Unsupported batch file type"):
        read_batch(path)


def test_names_are_filesystem_safe(tmp_path: Path) -> None:
    """Labels are reduced to characters that are valid in a path."""
    path = tmp_path / "points.csv"
    path.write_text("name,lat,lon\nA/B:C*D,38.8,27.0\n", encoding="utf-8")
    assert "/" not in read_csv(path)[0].name
