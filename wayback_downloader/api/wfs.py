"""Web Feature Service client.

WFS is the odd one out among the OGC services this tool speaks. WMS and WMTS
return pictures; WFS returns the features themselves -- geometry and attributes
as GeoJSON, GML, CSV or a zipped shapefile. Nothing is rendered, tiled or
mosaicked, so the response is written through exactly as received.

Three versions are in active use and they disagree about almost every name:

===============  ==============  ==============  =====================
Version          Type parameter  Limit           EPSG:4326 axis order
===============  ==============  ==============  =====================
1.0.0            ``typeName``    ``maxFeatures``  longitude, latitude
1.1.0            ``typeName``    ``maxFeatures``  latitude, longitude
2.0.0            ``typeNames``   ``count``        latitude, longitude
===============  ==============  ==============  =====================

Getting the axis order wrong returns features from the wrong part of the world
with a perfectly healthy HTTP 200, so the bounding box carries an explicit CRS
and is ordered to match the version.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Sequence

from wayback_downloader.api.ogc import _child, _children, _descendant, _text, _with_query
from wayback_downloader.exceptions import EndpointDiscoveryError, ValidationError
from wayback_downloader.models import BoundingBox
from wayback_downloader.utils.logger import get_logger

logger = get_logger(__name__)

SUPPORTED_VERSIONS = ("2.0.0", "1.1.0", "1.0.0")

# Equivalent spellings of the same output format, most preferred first.
#
# Services do not agree on a vocabulary, and the same server changes vocabulary
# with the version: this GeoServer's WFS 2.0.0 advertises `application/json` and
# `shape-zip`, while its 1.0.0 advertises `JSON` and `SHAPE-ZIP`. Matching a
# request against a whole family rather than one canonical spelling is what lets
# `--format geojson` work against either.
_FORMAT_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("application/json", "application/geo+json", "json", "geojson"),
    ("text/csv", "csv"),
    ("shape-zip", "shape", "shp", "application/zip"),
    (
        "application/vnd.google-earth.kml+xml",
        "application/vnd.google-earth.kml xml",
        "kml",
    ),
    (
        "application/gml+xml; version=3.2",
        "gml32",
        "gml3",
        "text/xml; subtype=gml/3.2",
        "text/xml; subtype=gml/3.1.1",
        "gml",
    ),
    ("gml2", "text/xml; subtype=gml/2.1.2"),
)

_ALIASES = {
    "geojson": "application/json",
    "json": "application/json",
    "csv": "text/csv",
    "shp": "shape-zip",
    "shape": "shape-zip",
    "kml": "application/vnd.google-earth.kml+xml",
    "gml": "gml3",
}


def _family_of(value: str) -> tuple[str, ...]:
    """Return the equivalence group a format spelling belongs to."""
    lowered = value.strip().lower()
    for family in _FORMAT_FAMILIES:
        if lowered in family:
            return family
    return ()


def resolve_output_format(requested: str | None, advertised: Sequence[str], default: str) -> str:
    """Choose the output format to request, in the service's own spelling.

    Matches exactly first, then across the equivalence family, so a request for
    ``geojson`` finds ``application/json`` on one service and ``JSON`` on
    another.
    """
    if requested is None:
        return default
    if not advertised:
        return normalize_output_format(requested)

    lowered = {option.strip().lower(): option for option in advertised}
    wanted = requested.strip().lower()
    if wanted in lowered:
        return lowered[wanted]

    for candidate in _family_of(wanted) or (normalize_output_format(requested).lower(),):
        if candidate in lowered:
            return lowered[candidate]

    raise ValidationError(
        f"Format {requested!r} is not offered here. Available: {', '.join(advertised)}"
    )


_EXTENSIONS = {
    "application/json": "geojson",
    "application/geo+json": "geojson",
    "json": "geojson",
    "geojson": "geojson",
    "csv": "csv",
    "text/csv": "csv",
    "shape-zip": "zip",
    "shape": "zip",
    "application/zip": "zip",
    # Both the MIME spelling and the bare name a 1.0.0 service advertises.
    "kml": "kml",
    "application/vnd.google-earth.kml+xml": "kml",
    "application/vnd.google-earth.kml xml": "kml",
    "application/vnd.google-earth.kmz": "kmz",
    "gml2": "gml",
    "gml3": "gml",
    "gml32": "gml",
    "application/gml+xml": "gml",
    "text/xml": "gml",
}


def normalize_output_format(value: str) -> str:
    """Expand a short output-format name into its most common spelling."""
    text = value.strip()
    if "/" in text:
        return text
    return _ALIASES.get(text.lower(), text)


def output_extension(output_format: str) -> str:
    """Return the file extension for a WFS output format."""
    base = output_format.split(";")[0].strip().lower()
    if base in _EXTENSIONS:
        return _EXTENSIONS[base]
    if "json" in base:
        return "geojson"
    if "gml" in base or "xml" in base:
        return "gml"
    if "csv" in base:
        return "csv"
    if "zip" in base or "shape" in base:
        return "zip"
    return "dat"


# Formats whose whole point is to be opened and looked at, where the absence of
# styling is a surprise rather than a detail.
_PRESENTATION_FORMATS = ("kml", "kmz", "google-earth")


def styling_note(output_format: str) -> str:
    """Warn that WFS output carries no styling, when the format implies looking at it.

    WFS serves features, not cartography: the response has geometry and
    attributes but no ``<Style>`` at all. Styling lives in the layer's SLD,
    which only WMS applies. Measured on the same layer and extent: the WFS KML
    had 20 placemarks with 20 ``ExtendedData`` blocks and zero style elements,
    while the WMS KML had 103 placemarks with 103 ``LineStyle`` elements in
    four colours and no attributes -- so the two are a genuine trade-off, not a
    ranking.
    """
    lowered = output_format.lower()
    if not any(marker in lowered for marker in _PRESENTATION_FORMATS):
        return ""
    return (
        "WFS output carries no styling -- it has the features and their "
        "attributes, but the colours and line widths live in the layer's SLD, "
        "which only WMS applies. For a styled KML use: wms <url> --layers "
        "<layer> --format kml (that route omits the attributes instead)."
    )


def short_crs(identifier: str) -> str:
    """Reduce a CRS identifier to its familiar short form.

    Services name the same system as ``EPSG:4326``, ``urn:ogc:def:crs:EPSG::4326``
    or ``http://www.opengis.net/def/crs/EPSG/0/4326``; only the last part is
    worth showing.
    """
    text = identifier.strip()
    if not text:
        return "-"
    if text.lower().startswith(("urn:", "http://", "https://")):
        parts = [part for part in text.replace("/", ":").split(":") if part]
        code = parts[-1] if parts else text
        authority = "EPSG" if "epsg" in text.lower() else parts[-2] if len(parts) > 1 else ""
        return f"{authority}:{code}" if authority else code
    return text


@dataclass(frozen=True)
class WfsFeatureType:
    """One feature type advertised by a WFS service."""

    name: str
    title: str
    crs: tuple[str, ...]
    bounds: BoundingBox | None
    abstract: str = ""

    @property
    def default_crs(self) -> str:
        """The first advertised CRS, in short form."""
        return short_crs(self.crs[0]) if self.crs else "-"


@dataclass
class WfsCapabilities:
    """The parts of a WFS capabilities document this downloader needs."""

    service_url: str
    title: str
    version: str
    feature_types: list[WfsFeatureType] = field(default_factory=list)
    formats: tuple[str, ...] = ()

    @property
    def default_format(self) -> str:
        """Prefer GeoJSON, which is the most immediately usable output."""
        for candidate in self.formats:
            if "json" in candidate.lower():
                return candidate
        return self.formats[0] if self.formats else "application/json"

    def feature_type(self, name: str) -> WfsFeatureType:
        """Return a feature type by name, case-insensitively."""
        for item in self.feature_types:
            if item.name == name:
                return item
        lowered = name.lower()
        for item in self.feature_types:
            if item.name.lower() == lowered:
                return item

        from wayback_downloader.api.ogc import suggest_layer

        names = [item.name for item in self.feature_types]
        available = ", ".join(names[:12]) + (" ..." if len(names) > 12 else "")
        raise ValidationError(
            f"Feature type {name!r} is not published by this service."
            f"{suggest_layer(name, names)} Available: {available}"
        )


def _corner_bounds(node: ET.Element) -> BoundingBox | None:
    """Read an ``ows:WGS84BoundingBox``, used by WFS 1.1.0 and 2.0.0."""
    box = _child(node, "WGS84BoundingBox")
    if box is None:
        return None
    lower = _text(_child(box, "LowerCorner")).split()
    upper = _text(_child(box, "UpperCorner")).split()
    if len(lower) < 2 or len(upper) < 2:
        return None
    try:
        # WGS84BoundingBox is always longitude-first, whatever the service's
        # other axis conventions are.
        return BoundingBox(
            west=float(lower[0]),
            south=float(lower[1]),
            east=float(upper[0]),
            north=float(upper[1]),
        )
    except (TypeError, ValueError):
        return None


def _legacy_bounds(node: ET.Element) -> BoundingBox | None:
    """Read a WFS 1.0.0 ``LatLongBoundingBox``, whose values are attributes."""
    box = _child(node, "LatLongBoundingBox")
    if box is None:
        return None
    try:
        return BoundingBox(
            west=float(box.get("minx", "")),
            south=float(box.get("miny", "")),
            east=float(box.get("maxx", "")),
            north=float(box.get("maxy", "")),
        )
    except (TypeError, ValueError):
        return None


def _output_formats(root: ET.Element) -> tuple[str, ...]:
    """Collect the output formats a service accepts for GetFeature."""
    formats: list[str] = []

    # 1.1.0 and 2.0.0 advertise them as an operation parameter.
    for operation in root.iter():
        if _text(_child(operation, "Identifier")) or operation.get("name") != "GetFeature":
            continue
        for parameter in _children(operation, "Parameter"):
            if parameter.get("name") not in {"outputFormat", "OutputFormat"}:
                continue
            allowed = _child(parameter, "AllowedValues") or parameter
            formats.extend(_text(value) for value in _children(allowed, "Value") if _text(value))

    # 1.0.0 lists them as empty elements under ResultFormat.
    result_format = _descendant(root, "ResultFormat")
    if result_format is not None:
        formats.extend(_local_name(child) for child in result_format)

    return tuple(dict.fromkeys(item for item in formats if item))


def _local_name(element: ET.Element) -> str:
    """Return an element's tag without its namespace."""
    return element.tag.rsplit("}", 1)[-1]


def parse_wfs_capabilities(xml: str, service_url: str) -> WfsCapabilities:
    """Parse a WFS ``GetCapabilities`` document of any supported version."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise EndpointDiscoveryError(
            f"Could not parse WFS capabilities from {service_url}: {exc}"
        ) from exc

    version = root.get("version") or "2.0.0"
    identification = _descendant(root, "ServiceIdentification") or _descendant(root, "Service")
    title = _text(_child(identification, "Title")) or "WFS service"

    capabilities = WfsCapabilities(
        service_url=service_url,
        title=title,
        version=version,
        formats=_output_formats(root),
    )

    for node in _children(_descendant(root, "FeatureTypeList"), "FeatureType"):
        name = _text(_child(node, "Name"))
        if not name:
            continue

        # DefaultSRS (1.1.0), DefaultCRS (2.0.0) and SRS (1.0.0) all mean the
        # same thing, and Other* adds the alternatives.
        crs = tuple(
            _text(child)
            for child in node
            if _local_name(child) in {"DefaultSRS", "DefaultCRS", "SRS", "OtherSRS", "OtherCRS"}
            and _text(child)
        )
        capabilities.feature_types.append(
            WfsFeatureType(
                name=name,
                title=_text(_child(node, "Title")) or name,
                crs=crs,
                bounds=_corner_bounds(node) or _legacy_bounds(node),
                abstract=_text(_child(node, "Abstract")),
            )
        )

    if not capabilities.feature_types:
        raise EndpointDiscoveryError(
            f"The WFS capabilities at {service_url} advertise no feature types."
        )
    return capabilities


# The CRS spelling that pins the axis order rather than leaving it to the
# server's interpretation. GeoServer treats the short `EPSG:4326` as the legacy
# longitude-first order and this URN as the authority-defined latitude-first
# one, so pairing the URN with latitude-first coordinates is unambiguous.
# Verified against a live service: the short code with latitude-first returned
# zero features and HTTP 200, silently.
BBOX_CRS_URN = "urn:ogc:def:crs:EPSG::4326"


def bbox_parameter(bbox: BoundingBox, version: str) -> str:
    """Build the ``bbox`` parameter with an explicit, unambiguous CRS.

    WFS 1.0.0 predates the URN convention and is longitude-first; 1.1.0 and
    2.0.0 use the URN and are latitude-first. The CRS is always appended, since
    the same numbers mean different places without it.
    """
    if version == "1.0.0":
        return f"{bbox.west},{bbox.south},{bbox.east},{bbox.north},EPSG:4326"
    return f"{bbox.south},{bbox.west},{bbox.north},{bbox.east},{BBOX_CRS_URN}"


def wfs_getfeature_url(
    service_url: str,
    type_name: str,
    version: str = "2.0.0",
    bbox: BoundingBox | None = None,
    output_format: str | None = None,
    max_features: int | None = None,
    start_index: int | None = None,
    srs_name: str = "EPSG:4326",
    cql_filter: str | None = None,
    sort_by: str | None = None,
    property_names: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Build a WFS ``GetFeature`` URL for the given version.

    The bounding box carries its CRS explicitly and is ordered to match the
    version's axis convention: 1.0.0 is longitude-first, 1.1.0 and 2.0.0 are
    latitude-first for EPSG:4326. Sending the wrong order returns features from
    somewhere else with a healthy HTTP 200.
    """
    if version not in SUPPORTED_VERSIONS:
        raise ValidationError(
            f"Unsupported WFS version {version!r}; use one of {', '.join(SUPPORTED_VERSIONS)}."
        )

    is_v2 = version.startswith("2.")
    params: dict[str, Any] = {
        "SERVICE": "WFS",
        "VERSION": version,
        "REQUEST": "GetFeature",
        "typeNames" if is_v2 else "typeName": type_name,
        "srsName": srs_name,
    }

    if output_format:
        params["outputFormat"] = output_format
    if max_features is not None:
        if max_features <= 0:
            raise ValidationError("The feature limit must be a positive integer.")
        params["count" if is_v2 else "maxFeatures"] = max_features
    if start_index is not None:
        if start_index < 0:
            raise ValidationError("The start index cannot be negative.")
        params["startIndex"] = start_index

    if bbox is not None:
        params["bbox"] = bbox_parameter(bbox, version)

    if cql_filter:
        params["CQL_FILTER"] = cql_filter
    if sort_by:
        params["sortBy"] = sort_by
    if property_names:
        params["propertyName"] = property_names
    if extra:
        params.update(extra)

    return _with_query(service_url, params)


def summarize_features(payload: bytes, output_format: str) -> dict[str, Any]:
    """Describe what came back, as far as the format allows.

    Only GeoJSON is inspected; GML and shapefile archives are written through
    without being parsed, so their contents stay unreported rather than guessed
    at.
    """
    summary: dict[str, Any] = {"byte_size": len(payload)}
    if "json" not in output_format.lower():
        return summary

    try:
        document = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return summary

    features = document.get("features")
    if isinstance(features, list):
        summary["feature_count"] = len(features)
        geometry_types = {
            feature.get("geometry", {}).get("type")
            for feature in features
            if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)
        }
        summary["geometry_types"] = sorted(t for t in geometry_types if t)
        if features and isinstance(features[0], dict):
            summary["properties"] = sorted(features[0].get("properties", {}) or {})

    for key in ("numberMatched", "numberReturned", "totalFeatures"):
        if key in document:
            summary[key] = document[key]
    return summary
