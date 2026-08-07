"""Tests for splicing feature attributes into a styled KML."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from wayback_downloader.api.ogc import sibling_service_url
from wayback_downloader.exceptions import ExportError
from wayback_downloader.export.kml import KML_NAMESPACE, merge_attributes

NS = f"{{{KML_NAMESPACE}}}"

STYLED_KML = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="{KML_NAMESPACE}"><Document>
  <Placemark id="LAYER.1">
    <name>LAYER.1</name>
    <Style><LineStyle><color>ff0a0a91</color><width>2.0</width></LineStyle></Style>
    <LineString><coordinates>26.9,39.0 27.0,39.1</coordinates></LineString>
  </Placemark>
  <Placemark id="LAYER.2">
    <name>LAYER.2</name>
    <Style><LineStyle><color>ff620666</color><width>1.0</width></LineStyle></Style>
    <LineString><coordinates>26.5,38.5 26.6,38.6</coordinates></LineString>
  </Placemark>
</Document></kml>
""".encode()


def geojson(*features: tuple[str, dict]) -> bytes:
    """Build a FeatureCollection from (id, properties) pairs."""
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": identifier, "properties": properties}
                for identifier, properties in features
            ],
        }
    ).encode()


def placemarks(payload: bytes) -> list[ET.Element]:
    """Return the placemarks of a KML document."""
    root = ET.fromstring(payload)
    return [element for element in root.iter() if element.tag == f"{NS}Placemark"]


def attributes(placemark: ET.Element) -> dict[str, str | None]:
    """Read a placemark's ExtendedData back into a dict."""
    extended = next((c for c in placemark if c.tag == f"{NS}ExtendedData"), None)
    if extended is None:
        return {}
    return {
        data.get("name", ""): next((v.text for v in data if v.tag == f"{NS}value"), None)
        for data in extended
    }


def test_attributes_are_attached_to_matching_placemarks() -> None:
    """Each placemark gains the properties of the feature sharing its id."""
    merged, report = merge_attributes(
        STYLED_KML,
        geojson(("LAYER.1", {"FAYTIPI": 2, "FAYADI": "Izmir"}), ("LAYER.2", {"FAYTIPI": 4})),
    )

    assert report.matched == 2
    assert report.placemarks == 2
    assert report.complete is True
    assert report.attributes_added == 3

    first, second = placemarks(merged)
    assert attributes(first) == {"FAYTIPI": "2", "FAYADI": "Izmir"}
    assert attributes(second) == {"FAYTIPI": "4"}


def test_styling_is_left_untouched() -> None:
    """The merge must not disturb what made the KML render.

    Verified against live data too: style, colour and width counts were
    identical before and after.
    """
    merged, _ = merge_attributes(STYLED_KML, geojson(("LAYER.1", {"a": 1})))
    text = merged.decode()

    assert text.count("<LineStyle") == STYLED_KML.decode().count("<LineStyle")
    assert "ff0a0a91" in text and "ff620666" in text
    assert text.count("<width>") == 2


def test_geometry_survives() -> None:
    """Coordinates are preserved exactly."""
    merged, _ = merge_attributes(STYLED_KML, geojson(("LAYER.1", {"a": 1})))
    coordinates = [
        element.text
        for element in ET.fromstring(merged).iter()
        if element.tag == f"{NS}coordinates"
    ]
    assert coordinates == ["26.9,39.0 27.0,39.1", "26.5,38.5 26.6,38.6"]


def test_kml_namespace_is_preserved() -> None:
    """A KML that lost its namespace would not open in any viewer."""
    merged, _ = merge_attributes(STYLED_KML, geojson(("LAYER.1", {"a": 1})))
    assert ET.fromstring(merged).tag == f"{NS}kml"
    assert KML_NAMESPACE.encode() in merged


def test_unmatched_placemarks_are_reported_not_dropped() -> None:
    """A placemark with no matching feature stays, without attributes."""
    merged, report = merge_attributes(STYLED_KML, geojson(("LAYER.1", {"a": 1})))

    assert report.matched == 1
    assert report.unmatched_placemarks == 1
    assert report.complete is False

    first, second = placemarks(merged)
    assert attributes(first) == {"a": "1"}
    assert attributes(second) == {}


def test_name_is_used_when_there_is_no_id_attribute() -> None:
    """Some services identify a placemark only by its name element."""
    without_id = STYLED_KML.replace(b'<Placemark id="LAYER.1">', b"<Placemark>")
    merged, report = merge_attributes(without_id, geojson(("LAYER.1", {"a": 1})))
    assert report.matched == 1
    assert attributes(placemarks(merged)[0]) == {"a": "1"}


def test_null_property_becomes_an_empty_value() -> None:
    """A null attribute is kept as an empty value rather than dropped."""
    merged, _ = merge_attributes(STYLED_KML, geojson(("LAYER.1", {"a": None, "b": 0})))
    assert set(attributes(placemarks(merged)[0])) == {"a", "b"}


def test_merging_twice_is_idempotent() -> None:
    """Re-merging replaces the attributes rather than duplicating them."""
    once, _ = merge_attributes(STYLED_KML, geojson(("LAYER.1", {"a": 1})))
    twice, report = merge_attributes(once, geojson(("LAYER.1", {"a": 2})))

    assert report.matched == 1
    assert twice.decode().count("<ExtendedData") == 1
    assert attributes(placemarks(twice)[0]) == {"a": "2"}


def test_nothing_matching_is_an_error() -> None:
    """Merging unrelated documents fails loudly rather than writing a no-op."""
    with pytest.raises(ExportError, match="No placemark could be matched"):
        merge_attributes(STYLED_KML, geojson(("OTHER.9", {"a": 1})))


def test_per_request_identifiers_are_named_as_the_cause() -> None:
    """The usual reason a merge fails is an unstable id, not a missing one.

    A layer published without a primary key gets a fresh ``fid-...`` from
    GeoServer on every request, so the styled KML and the feature query label
    the same feature differently. Reporting that as "may not set placemark ids"
    sent the reader looking for the wrong thing.
    """
    volatile = STYLED_KML.replace(b"LAYER.1", b"vel_stations.fid-50c51ad1_52e8").replace(
        b"LAYER.2", b"vel_stations.fid-50c51ad1_52e9"
    )
    with pytest.raises(ExportError) as excinfo:
        merge_attributes(volatile, geojson(("vel_stations.fid-99999999_0001", {"a": 1})))

    message = str(excinfo.value)
    assert "per-request temporary ids" in message
    assert "primary key" in message
    assert "wfs" in message


def test_placemarks_without_identifiers_say_so() -> None:
    """A service that labels nothing gets its own explanation."""
    anonymous = STYLED_KML.replace(b'<Placemark id="LAYER.1">', b"<Placemark>").replace(
        b'<Placemark id="LAYER.2">', b"<Placemark>"
    )
    anonymous = anonymous.replace(b"<name>LAYER.1</name>", b"").replace(
        b"<name>LAYER.2</name>", b""
    )
    with pytest.raises(ExportError, match="No placemark carries an identifier"):
        merge_attributes(anonymous, geojson(("LAYER.1", {"a": 1})))


def test_invalid_inputs_are_reported() -> None:
    """Malformed input on either side names which side was wrong."""
    with pytest.raises(ExportError, match="styled KML could not be parsed"):
        merge_attributes(b"<kml", geojson(("LAYER.1", {"a": 1})))

    with pytest.raises(ExportError, match="not valid GeoJSON"):
        merge_attributes(STYLED_KML, b"{not json")


def test_kml_without_placemarks_is_reported() -> None:
    """An empty document is not silently accepted."""
    empty = f'<?xml version="1.0"?><kml xmlns="{KML_NAMESPACE}"><Document/></kml>'.encode()
    with pytest.raises(ExportError, match="no placemarks"):
        merge_attributes(empty, geojson(("LAYER.1", {"a": 1})))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x/geoserver/wms", "https://x/geoserver/wfs"),
        ("https://x/geoserver/mta/wms", "https://x/geoserver/mta/wfs"),
        ("https://x/geoserver/wms/", "https://x/geoserver/wfs"),
        ("https://x/geoserver/ows", "https://x/geoserver/ows"),
        ("https://x/some/other/path", "https://x/some/other/path"),
    ],
)
def test_companion_endpoint_is_derived(url: str, expected: str) -> None:
    """The WFS endpoint usually sits beside the WMS one.

    ``ows`` serves every service and is left alone; an unfamiliar path is
    returned unchanged rather than mangled, since --wfs-url can override it.
    """
    assert sibling_service_url(url, "wfs") == expected


def test_query_parameters_survive_the_swap() -> None:
    """An endpoint carrying a map or token keeps it."""
    assert sibling_service_url("https://x/geoserver/wms?map=/d/x.map", "wfs") == (
        "https://x/geoserver/wfs?map=/d/x.map"
    )
