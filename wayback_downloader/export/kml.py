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
from typing import Any

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

    @property
    def complete(self) -> bool:
        """Whether every placemark received its attributes."""
        return self.placemarks > 0 and self.unmatched_placemarks == 0

    def summary(self) -> str:
        """A one-line description for logs and the CLI."""
        return (
            f"{self.matched}/{self.placemarks} placemark(s) matched a feature, "
            f"{self.attributes_added} attribute value(s) attached"
        )


def _local(tag: str) -> str:
    """Return an element tag without its namespace."""
    return tag.rsplit("}", 1)[-1]


def _feature_properties(geojson: bytes) -> dict[str, dict[str, Any]]:
    """Index a GeoJSON FeatureCollection's properties by feature id."""
    try:
        document = json.loads(geojson)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ExportError(f"The feature data is not valid GeoJSON: {exc}") from exc

    features = document.get("features")
    if not isinstance(features, list):
        raise ExportError("The feature data contains no FeatureCollection features.")

    indexed: dict[str, dict[str, Any]] = {}
    for feature in features:
        if not isinstance(feature, dict):
            continue
        identifier = feature.get("id")
        properties = feature.get("properties")
        if identifier and isinstance(properties, dict):
            indexed[str(identifier)] = properties
    return indexed


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


def _no_match_reason(placemarks: list[ET.Element], properties_by_id: dict[str, Any]) -> str:
    """Explain why nothing joined, distinguishing the causes that look alike.

    The common one is not a missing identifier but an unstable one: for a layer
    without a primary key GeoServer mints a fresh ``fid-...`` per request, so
    the WMS and WFS responses name the same feature differently and no join is
    possible however the two are fetched.
    """
    kml_ids = [identifier for placemark in placemarks if (identifier := _placemark_id(placemark))]
    if not kml_ids:
        return (
            "No placemark carries an identifier, so there is nothing to match the "
            "features against. This service does not label its placemarks."
        )

    volatile = sum(1 for identifier in kml_ids if ".fid-" in identifier)
    if volatile and volatile == len(kml_ids):
        return (
            "The placemark identifiers are per-request temporary ids (fid-...), which "
            "the service regenerates for every call, so the styled KML and the feature "
            "query name the same features differently and cannot be joined. This "
            "happens for layers published without a primary key. Download the two "
            "separately instead: `wms --format kml` for the styling, `wfs` for the "
            "attributes."
        )
    if not properties_by_id:
        return "The feature response carried no identified features to match against."
    return (
        "No placemark could be matched to a feature by id. The two responses may "
        "describe different layers or different extents."
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

    properties_by_id = _feature_properties(geojson)
    placemarks = [element for element in root.iter() if _local(element.tag) == "Placemark"]
    if not placemarks:
        raise ExportError("The styled KML contains no placemarks to annotate.")

    matched = 0
    attributes_added = 0

    for placemark in placemarks:
        identifier = _placemark_id(placemark)
        properties = properties_by_id.get(identifier or "")
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
        features=len(properties_by_id),
        matched=matched,
        unmatched_placemarks=len(placemarks) - matched,
        attributes_added=attributes_added,
    )
    if matched == 0:
        raise ExportError(_no_match_reason(placemarks, properties_by_id))
    if not report.complete:
        logger.warning(
            "%d placemark(s) had no matching feature and carry no attributes; "
            "widen --max-features so the feature query covers the same extent",
            report.unmatched_placemarks,
        )

    payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return payload, report
