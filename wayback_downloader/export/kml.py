"""Combining a styled KML with the attributes of the same features.

Neither OGC service gives you both halves. WMS renders the layer through its
SLD, so its KML carries ``<Style>`` elements and puts the attributes in a
human-readable ``<description>`` balloon. WFS serves the features, so its KML
carries machine-readable attributes and no styling at all.

The two describe the same features and, usefully, agree on their identifiers:
GeoServer writes ``<Placemark id="LAYER.134">`` and the matching GeoJSON
feature is ``"id": "LAYER.134"``. Measured on one layer and extent, every
placemark matched a feature and every feature matched a placemark. That makes
the merge exact rather than a geometric guess: the styled KML is kept whole and
each placemark gains an ``<ExtendedData>`` block built from its feature's
properties.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Iterable

from wayback_downloader.exceptions import ExportError
from wayback_downloader.utils.logger import get_logger

logger = get_logger(__name__)

KML_NAMESPACE = "http://www.opengis.net/kml/2.2"


@dataclass
class MergeReport:
    """How completely the attributes could be attached."""

    placemarks: int
    features: int
    matched: int
    unmatched_placemarks: int
    attributes_added: int
    # Which join actually worked: "id" or "geometry".
    strategy: str = "id"

    @property
    def complete(self) -> bool:
        """Whether every placemark received its attributes."""
        return self.placemarks > 0 and self.unmatched_placemarks == 0

    def summary(self) -> str:
        """A one-line description for logs and the CLI."""
        return (
            f"{self.matched}/{self.placemarks} placemark(s) matched a feature "
            f"by {self.strategy}, {self.attributes_added} attribute value(s) attached"
        )


def _local(tag: str) -> str:
    """Return an element tag without its namespace."""
    return tag.rsplit("}", 1)[-1]


def _index_features(
    geojson: bytes,
) -> tuple[dict[str, dict[str, Any]], dict[GeometryKey, dict[str, Any]]]:
    """Index a FeatureCollection's properties by id and, separately, by geometry.

    Both indexes are built because either can be the useless one: a layer with a
    primary key has stable ids, a layer without one has ids that change per
    request but geometry that does not.
    """
    try:
        document = json.loads(geojson)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ExportError(f"The feature data is not valid GeoJSON: {exc}") from exc

    features = document.get("features")
    if not isinstance(features, list):
        raise ExportError("The feature data contains no FeatureCollection features.")

    by_id: dict[str, dict[str, Any]] = {}
    by_geometry: dict[GeometryKey, dict[str, Any]] = {}
    ambiguous: set[GeometryKey] = set()

    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            continue

        identifier = feature.get("id")
        if identifier:
            by_id[str(identifier)] = properties

        key = _geojson_geometry_key(feature.get("geometry"))
        if not key:
            continue
        # Two features at identical coordinates cannot be told apart, so neither
        # is matched rather than attaching one's attributes to the other.
        if key in by_geometry or key in ambiguous:
            ambiguous.add(key)
            by_geometry.pop(key, None)
        else:
            by_geometry[key] = properties

    if ambiguous:
        logger.debug("%d geometry key(s) matched more than one feature", len(ambiguous))
    return by_id, by_geometry


# Coordinates are compared at roughly centimetre precision. The two services
# describe identical positions but format them differently -- 42.709830000000004
# against 42.70983 -- so exact string equality matches almost nothing while
# rounding matches everything.
_COORDINATE_PRECISION = 7

GeometryKey = tuple[tuple[float, float], ...]


def _round_pair(lon: float, lat: float) -> tuple[float, float]:
    """Reduce a coordinate to the precision the two services agree on."""
    return round(lon, _COORDINATE_PRECISION), round(lat, _COORDINATE_PRECISION)


def _geometry_key(points: Iterable[tuple[float, float]]) -> GeometryKey:
    """Build a comparable key from a geometry's coordinates.

    Sorted rather than ordered, because the two encodings may wind a ring or
    split a multi-geometry differently while covering the same positions.
    """
    return tuple(sorted(points))


def _kml_geometry_key(placemark: ET.Element) -> GeometryKey:
    """Extract a placemark's coordinates as a comparable key."""
    points: list[tuple[float, float]] = []
    for element in placemark.iter():
        if _local(element.tag) != "coordinates" or not element.text:
            continue
        for token in element.text.split():
            parts = token.split(",")
            if len(parts) >= 2:
                try:
                    points.append(_round_pair(float(parts[0]), float(parts[1])))
                except ValueError:
                    continue
    return _geometry_key(points)


def _geojson_geometry_key(geometry: Any) -> GeometryKey:
    """Extract a GeoJSON geometry's coordinates as a comparable key."""
    if not isinstance(geometry, dict):
        return ()

    points: list[tuple[float, float]] = []
    stack = [geometry.get("coordinates")]
    while stack:
        item = stack.pop()
        if not isinstance(item, (list, tuple)):
            continue
        if len(item) >= 2 and all(isinstance(value, (int, float)) for value in item[:2]):
            points.append(_round_pair(float(item[0]), float(item[1])))
        else:
            stack.extend(item)
    return _geometry_key(points)


def _placemark_id(placemark: ET.Element) -> str | None:
    """Return a placemark's identifier.

    Prefers the ``id`` attribute and falls back to ``<name>``, which GeoServer
    sets to the same value.
    """
    identifier = placemark.get("id")
    if identifier:
        return identifier
    for child in placemark:
        if _local(child.tag) == "name" and (child.text or "").strip():
            return (child.text or "").strip()
    return None


def _no_match_reason(
    placemarks: list[ET.Element],
    by_id: dict[str, Any],
    by_geometry: dict[GeometryKey, Any],
) -> str:
    """Explain why nothing joined, once both strategies have failed."""
    if not by_id and not by_geometry:
        return "The feature response carried no usable features to match against."

    if not any(_kml_geometry_key(placemark) for placemark in placemarks):
        return (
            "The placemarks carry no coordinates, so they cannot be matched by "
            "geometry, and their identifiers matched no feature."
        )

    return (
        "No placemark could be matched to a feature, by identifier or by coordinates. "
        "The two responses most likely cover different extents or different layers -- "
        "check that the feature query was not narrowed by --max-features or --filter."
    )


def merge_attributes(styled_kml: bytes, geojson: bytes) -> tuple[bytes, MergeReport]:
    """Attach GeoJSON attributes to a styled KML, matched on feature id.

    The KML is preserved as it arrived -- styles, descriptions, geometry -- and
    only gains an ``ExtendedData`` block per matched placemark, so nothing that
    made it render correctly is disturbed.
    """
    ET.register_namespace("", KML_NAMESPACE)
    try:
        root = ET.fromstring(styled_kml)
    except ET.ParseError as exc:
        raise ExportError(f"The styled KML could not be parsed: {exc}") from exc

    by_id, by_geometry = _index_features(geojson)
    placemarks = [element for element in root.iter() if _local(element.tag) == "Placemark"]
    if not placemarks:
        raise ExportError("The styled KML contains no placemarks to annotate.")

    # Identifiers are preferred when they work: they are exact and cheap. But a
    # layer published without a primary key gets a fresh `fid-...` per request,
    # so the two responses label the same feature differently while describing
    # the same coordinates. Deciding by trial rather than by guessing which case
    # applies keeps both kinds of layer working.
    strategy = "id"
    if not any(by_id.get(_placemark_id(mark) or "") for mark in placemarks):
        strategy = "geometry"
        logger.info("Placemark ids did not match any feature; matching on geometry instead")

    matched = 0
    attributes_added = 0

    for placemark in placemarks:
        if strategy == "id":
            properties = by_id.get(_placemark_id(placemark) or "")
        else:
            properties = by_geometry.get(_kml_geometry_key(placemark))
        if not properties:
            continue

        # Replace rather than append, so re-merging a file stays idempotent.
        for existing in [c for c in placemark if _local(c.tag) == "ExtendedData"]:
            placemark.remove(existing)

        extended = ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}ExtendedData")
        for key, value in properties.items():
            data = ET.SubElement(extended, f"{{{KML_NAMESPACE}}}Data", {"name": str(key)})
            ET.SubElement(data, f"{{{KML_NAMESPACE}}}value").text = (
                "" if value is None else str(value)
            )
            attributes_added += 1
        matched += 1

    report = MergeReport(
        placemarks=len(placemarks),
        features=len(by_id if strategy == "id" else by_geometry),
        strategy=strategy,
        matched=matched,
        unmatched_placemarks=len(placemarks) - matched,
        attributes_added=attributes_added,
    )
    if matched == 0:
        raise ExportError(_no_match_reason(placemarks, by_id, by_geometry))
    if not report.complete:
        logger.warning(
            "%d placemark(s) had no matching feature and carry no attributes; "
            "widen --max-features so the feature query covers the same extent",
            report.unmatched_placemarks,
        )

    payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return payload, report
