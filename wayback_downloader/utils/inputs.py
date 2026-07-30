"""Batch input parsing for CSV and GeoJSON coordinate sources."""

from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wayback_downloader.exceptions import ValidationError
from wayback_downloader.models import Coordinate
from wayback_downloader.utils.logger import get_logger
from wayback_downloader.utils.naming import safe_stem
from wayback_downloader.utils.validator import validate_coordinate, validate_date

logger = get_logger(__name__)

_LAT_KEYS = ("lat", "latitude", "y", "enlem")
_LON_KEYS = ("lon", "lng", "long", "longitude", "x", "boylam")
_DATE_KEYS = ("date", "tarih", "requested_date")
_NAME_KEYS = ("name", "id", "label", "isim", "ad")
_ZOOM_KEYS = ("zoom", "z", "level")


@dataclass(frozen=True)
class BatchEntry:
    """One row of a batch job."""

    coordinate: Coordinate
    name: str
    date: dt.date | None = None
    zoom: int | None = None


def _lookup(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first present value among case-insensitive column aliases."""
    normalized = {str(key).strip().lower(): value for key, value in row.items() if key}
    for key in keys:
        value = normalized.get(key)
        if value not in (None, ""):
            return value
    return None


def _build_entry(row: dict[str, Any], index: int, source: str) -> BatchEntry:
    """Convert one parsed row into a validated batch entry."""
    latitude = _lookup(row, _LAT_KEYS)
    longitude = _lookup(row, _LON_KEYS)
    if latitude is None or longitude is None:
        raise ValidationError(
            f"{source} row {index}: could not find latitude/longitude columns. "
            f"Expected one of {_LAT_KEYS} and {_LON_KEYS}."
        )

    try:
        coordinate = validate_coordinate(float(latitude), float(longitude))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{source} row {index}: non-numeric coordinate") from exc

    raw_date = _lookup(row, _DATE_KEYS)
    raw_zoom = _lookup(row, _ZOOM_KEYS)
    name = _lookup(row, _NAME_KEYS) or f"point_{index:03d}"

    return BatchEntry(
        coordinate=coordinate,
        name=_sanitize_name(str(name)),
        date=validate_date(str(raw_date)) if raw_date else None,
        zoom=int(raw_zoom) if raw_zoom else None,
    )


def _sanitize_name(name: str) -> str:
    """Reduce a label to characters that are safe in a filename."""
    return safe_stem(name, fallback="point")


def read_csv(path: Path) -> list[BatchEntry]:
    """Read coordinates from a CSV file with a header row."""
    if not path.exists():
        raise ValidationError(f"CSV file not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.DictReader(handle, dialect=dialect))

    if not rows:
        raise ValidationError(f"CSV file {path} contains no data rows.")
    return [_build_entry(row, index, path.name) for index, row in enumerate(rows, start=1)]


def read_geojson(path: Path) -> list[BatchEntry]:
    """Read Point features from a GeoJSON file.

    Non-point geometries are represented by their first coordinate pair, which
    is enough to anchor a download window.
    """
    if not path.exists():
        raise ValidationError(f"GeoJSON file not found: {path}")

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Could not parse GeoJSON {path}: {exc}") from exc

    if document.get("type") == "FeatureCollection":
        features = document.get("features") or []
    elif document.get("type") == "Feature":
        features = [document]
    else:
        features = [{"type": "Feature", "geometry": document, "properties": {}}]

    entries: list[BatchEntry] = []
    for index, feature in enumerate(features, start=1):
        geometry = feature.get("geometry") or {}
        position = _first_position(geometry.get("coordinates"))
        if position is None:
            logger.warning("Skipping GeoJSON feature %d: no usable coordinates", index)
            continue

        row = dict(feature.get("properties") or {})
        row["lon"], row["lat"] = position[0], position[1]
        entries.append(_build_entry(row, index, path.name))

    if not entries:
        raise ValidationError(f"GeoJSON file {path} contained no usable point geometries.")
    return entries


def _first_position(coordinates: Any) -> tuple[float, float] | None:
    """Recursively extract the first ``[lon, lat]`` pair from a coordinate array."""
    if not isinstance(coordinates, (list, tuple)) or not coordinates:
        return None
    head = coordinates[0]
    if isinstance(head, (int, float)) and len(coordinates) >= 2:
        return float(coordinates[0]), float(coordinates[1])
    return _first_position(head)


def read_batch(path: Path) -> list[BatchEntry]:
    """Read a batch file, dispatching on its extension."""
    suffix = path.suffix.lower()
    if suffix in {".geojson", ".json"}:
        return read_geojson(path)
    if suffix in {".csv", ".tsv", ".txt"}:
        return read_csv(path)
    raise ValidationError(
        f"Unsupported batch file type {suffix!r}. Use .csv, .tsv, .json or .geojson."
    )
