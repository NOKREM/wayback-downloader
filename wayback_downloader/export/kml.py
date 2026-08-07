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
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Iterable

from wayback_downloader.exceptions import ExportError
from wayback_downloader.export.sld import RuleStyle, classify, parse_sld, rule_style
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


# GeoServer renders each placemark's attributes into an HTML balloon inside
# <description>, as `<span class="atr-name">FAY_ADI</span> ... <span
# class="atr-value">CESME FAYI</span>`. That is the same data WFS would return,
# already present in the styled KML, which is what makes a merge possible on a
# server with WFS switched off.
_BALLOON_ATTRIBUTE = re.compile(
    r'atr-name"?>(?P<name>.*?)</span>.*?atr-value"?>(?P<value>.*?)</span>',
    re.DOTALL,
)

# GeoServer writes a literal <Null> for an absent value.
_BALLOON_NULL = "<Null>"


def _description_attributes(placemark: ET.Element) -> dict[str, str]:
    """Read a placemark's attributes out of its own description balloon."""
    description = next((child for child in placemark if _local(child.tag) == "description"), None)
    if description is None or not description.text:
        return {}

    found: dict[str, str] = {}
    for match in _BALLOON_ATTRIBUTE.finditer(description.text):
        name = unescape(match.group("name")).strip()
        value = unescape(match.group("value")).strip()
        if name:
            found[name] = "" if value == _BALLOON_NULL else value
    return found


def attributes_from_descriptions(styled_kml: bytes) -> tuple[bytes, MergeReport]:
    """Turn each placemark's description balloon into machine-readable data.

    Needed when the server publishes no WFS -- MTA's GeoServer answers a
    capabilities request with ``ServiceUnavailable`` -- since there is then no
    second response to join against. The attributes are not missing in that
    case, only unstructured: they sit in the HTML balloon WMS already produced,
    so they can be promoted to ``ExtendedData`` without another request.
    """
    ET.register_namespace("", KML_NAMESPACE)
    try:
        root = ET.fromstring(styled_kml)
    except ET.ParseError as exc:
        raise ExportError(f"The styled KML could not be parsed: {exc}") from exc

    placemarks = [element for element in root.iter() if _local(element.tag) == "Placemark"]
    if not placemarks:
        raise ExportError("The styled KML contains no placemarks to annotate.")

    matched = 0
    attributes_added = 0
    for placemark in placemarks:
        found = _description_attributes(placemark)
        if not found:
            continue
        _attach(placemark, found)
        matched += 1
        attributes_added += len(found)

    if matched == 0:
        raise ExportError(
            "No placemark carries an attribute balloon in its description, so there "
            "is nothing to promote. This service renders its KML without attributes."
        )

    return (
        ET.tostring(root, encoding="utf-8", xml_declaration=True),
        MergeReport(
            placemarks=len(placemarks),
            features=matched,
            matched=matched,
            unmatched_placemarks=len(placemarks) - matched,
            attributes_added=attributes_added,
            strategy="description",
        ),
    )


@dataclass
class LegendReport:
    """How the stylesheet's rules mapped onto the placemarks."""

    placemarks: int
    labelled: int
    groups: dict[str, int] = field(default_factory=dict)
    restyled: int = 0
    colours: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        """A one-line description for logs and the CLI."""
        listed = ", ".join(f"{name} ({count})" for name, count in self.groups.items())
        recoloured = f", {self.restyled} restyled" if self.restyled else ""
        return f"{self.labelled}/{self.placemarks} placemark(s) classified{recoloured}: {listed}"


def _style_id(label: str) -> str:
    """Build a KML style id from a rule name."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-").lower()
    return f"rule-{slug or 'unnamed'}"


def _build_style(style_id: str, style: RuleStyle) -> ET.Element:
    """Build a shared ``<Style>`` element from a rule's drawing instructions."""
    element = ET.Element(f"{{{KML_NAMESPACE}}}Style", {"id": style_id})
    if style.line_colour or style.line_width:
        line = ET.SubElement(element, f"{{{KML_NAMESPACE}}}LineStyle")
        if style.line_colour:
            ET.SubElement(line, f"{{{KML_NAMESPACE}}}color").text = style.line_colour
        if style.line_width:
            ET.SubElement(line, f"{{{KML_NAMESPACE}}}width").text = style.line_width
    if style.fill_colour:
        poly = ET.SubElement(element, f"{{{KML_NAMESPACE}}}PolyStyle")
        ET.SubElement(poly, f"{{{KML_NAMESPACE}}}color").text = style.fill_colour
    return element


def _restyle(placemark: ET.Element, style_id: str) -> None:
    """Point a placemark at a shared style instead of its inline one."""
    for inline in [c for c in placemark if _local(c.tag) in {"Style", "styleUrl"}]:
        placemark.remove(inline)
    # KML requires styleUrl before the geometry, and ElementTree appends, so the
    # element is inserted at the front rather than added at the end.
    url = ET.Element(f"{{{KML_NAMESPACE}}}styleUrl")
    url.text = f"#{style_id}"
    placemark.insert(0, url)


def apply_stylesheet(
    styled_kml: bytes,
    stylesheet: bytes,
    attribute: str = "sld_rule",
    recolour: bool = True,
) -> tuple[bytes, LegendReport]:
    """Label each placemark with the stylesheet rule that selects it.

    The rule is decided by evaluating the SLD's filters against the placemark's
    own attributes, which requires those attributes to have been promoted into
    ``ExtendedData`` first. Placemarks are then grouped into a ``<Folder>`` per
    rule, which is what turns an anonymous KML into one a viewer can show as a
    legend with a togglable entry per category.

    The styling itself is left exactly as the server rendered it. This adds the
    names it left out; it does not re-render anything.
    """
    ET.register_namespace("", KML_NAMESPACE)
    try:
        root = ET.fromstring(styled_kml)
    except ET.ParseError as exc:
        raise ExportError(f"The styled KML could not be parsed: {exc}") from exc

    rules = parse_sld(stylesheet)
    documents = [element for element in root.iter() if _local(element.tag) == "Document"]
    if not documents:
        raise ExportError("The styled KML has no Document to organise.")
    document = documents[0]

    placemarks = [element for element in root.iter() if _local(element.tag) == "Placemark"]
    if not placemarks:
        raise ExportError("The styled KML contains no placemarks to classify.")

    grouped: dict[str, list[ET.Element]] = {}
    labelled = 0
    restyled = 0
    colours: dict[str, str] = {}
    shared: dict[str, RuleStyle] = {}

    for placemark in placemarks:
        attributes = _existing_attributes(placemark) or _description_attributes(placemark)
        rule = classify(rules, attributes) if attributes else None
        if rule is None:
            grouped.setdefault("Unclassified", []).append(placemark)
            continue

        labelled += 1
        grouped.setdefault(rule.label, []).append(placemark)
        _add_attribute(placemark, attribute, rule.label)

        # The server's KML paints every feature with the last matching rule's
        # symbolizer, which for a stylesheet ending in a catch-all means one
        # colour for the whole layer -- 660 of 660 grey on the tested layer,
        # where the raster rendering shows four. Repointing each placemark at
        # its own rule's style restores the distinction.
        if not recolour:
            continue
        style = rule_style(rule)
        if style.is_empty:
            continue

        style_id = _style_id(rule.label)
        shared[style_id] = style
        if style.line_colour:
            colours[rule.label] = style.line_colour
        _restyle(placemark, style_id)
        restyled += 1

    # Detach every placemark from wherever it sat, then re-add it under a folder
    # named for its rule. Parents are found by search because ElementTree has no
    # parent pointers.
    for parent in root.iter():
        for child in list(parent):
            if _local(child.tag) == "Placemark":
                parent.remove(child)

    # Whatever folders the document already had are now empty, since every
    # placemark was lifted out of them. Left in place they would show up as
    # phantom legend entries.
    _drop_empty_folders(root)

    # Shared styles have to precede the folders that reference them.
    for index, (style_id, style) in enumerate(shared.items()):
        document.insert(index, _build_style(style_id, style))

    for name, members in grouped.items():
        folder = ET.SubElement(document, f"{{{KML_NAMESPACE}}}Folder")
        ET.SubElement(folder, f"{{{KML_NAMESPACE}}}name").text = name
        for member in members:
            folder.append(member)

    report = LegendReport(
        placemarks=len(placemarks),
        labelled=labelled,
        groups={name: len(members) for name, members in grouped.items()},
        restyled=restyled,
        colours=colours,
    )
    if labelled == 0:
        raise ExportError(
            "No placemark matched any rule in the stylesheet. The attributes the "
            "rules filter on are probably absent -- run the merge so they are "
            "attached before the stylesheet is applied."
        )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), report


def _drop_empty_folders(root: ET.Element) -> int:
    """Remove folders left holding nothing but their own name."""
    removed = 0
    for parent in list(root.iter()):
        for child in list(parent):
            if _local(child.tag) != "Folder":
                continue
            if any(_local(grandchild.tag) != "name" for grandchild in child):
                continue
            parent.remove(child)
            removed += 1
    return removed


def _existing_attributes(placemark: ET.Element) -> dict[str, str]:
    """Read back attributes already attached as ExtendedData."""
    extended = next((c for c in placemark if _local(c.tag) == "ExtendedData"), None)
    if extended is None:
        return {}

    found: dict[str, str] = {}
    for data in extended:
        name = data.get("name")
        if not name:
            continue
        value = next((v.text for v in data if _local(v.tag) == "value"), None)
        found[name] = value or ""
    return found


def _add_attribute(placemark: ET.Element, name: str, value: str) -> None:
    """Add a single ExtendedData entry without disturbing the others."""
    extended = next((c for c in placemark if _local(c.tag) == "ExtendedData"), None)
    if extended is None:
        extended = ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}ExtendedData")

    for data in extended:
        if data.get("name") == name:
            extended.remove(data)

    data = ET.SubElement(extended, f"{{{KML_NAMESPACE}}}Data", {"name": name})
    ET.SubElement(data, f"{{{KML_NAMESPACE}}}value").text = value


def _attach(placemark: ET.Element, properties: dict[str, Any]) -> None:
    """Replace a placemark's ExtendedData with the given properties."""
    for existing in [c for c in placemark if _local(c.tag) == "ExtendedData"]:
        placemark.remove(existing)

    extended = ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}ExtendedData")
    for key, value in properties.items():
        data = ET.SubElement(extended, f"{{{KML_NAMESPACE}}}Data", {"name": str(key)})
        ET.SubElement(data, f"{{{KML_NAMESPACE}}}value").text = "" if value is None else str(value)


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

        # Replaces rather than appends, so re-merging a file stays idempotent.
        _attach(placemark, properties)
        attributes_added += len(properties)
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
