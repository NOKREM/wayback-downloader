"""Tests for splicing feature attributes into a styled KML."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from wayback_downloader.api.ogc import sibling_service_url
from wayback_downloader.exceptions import ExportError
from wayback_downloader.export.kml import (
    KML_NAMESPACE,
    attributes_from_descriptions,
    merge_attributes,
)

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


def geo_feature(identifier: str | None, properties: dict, coordinates: list) -> dict:
    """Build a GeoJSON feature with geometry."""
    feature: dict = {
        "type": "Feature",
        "properties": properties,
        "geometry": {"type": "LineString", "coordinates": coordinates},
    }
    if identifier:
        feature["id"] = identifier
    return feature


def collection(*features: dict) -> bytes:
    """Wrap features in a FeatureCollection."""
    return json.dumps({"type": "FeatureCollection", "features": list(features)}).encode()


def test_geometry_matches_when_identifiers_cannot() -> None:
    """A layer without a primary key still merges, via its coordinates.

    GeoServer mints a fresh ``fid-...`` per request for such layers, so the two
    responses label the same feature differently -- but they describe the same
    positions. Verified against a live layer: all 1670 placemarks matched.
    """
    volatile = STYLED_KML.replace(b"LAYER.1", b"last20days.fid-466b7a66_7bff").replace(
        b"LAYER.2", b"last20days.fid-466b7a66_7bfe"
    )
    features = collection(
        geo_feature("last20days.fid-DIFFERENT_0001", {"m": "4.2"}, [[26.9, 39.0], [27.0, 39.1]]),
        geo_feature("last20days.fid-DIFFERENT_0002", {"m": "3.1"}, [[26.5, 38.5], [26.6, 38.6]]),
    )

    merged, report = merge_attributes(volatile, features)
    assert report.strategy == "geometry"
    assert report.matched == 2

    first, second = placemarks(merged)
    assert attributes(first) == {"m": "4.2"}
    assert attributes(second) == {"m": "3.1"}


def test_geometry_match_attaches_each_feature_to_its_own_placemark() -> None:
    """Matching something is not enough; it has to be the right something.

    Confirmed on live data too: the layer carries its own latitude and
    longitude as attributes, and for all 1670 placemarks those values equalled
    the placemark's own coordinates exactly.
    """
    volatile = STYLED_KML.replace(b"LAYER.1", b"x.fid-1").replace(b"LAYER.2", b"x.fid-2")
    # Deliberately supplied in the opposite order to the placemarks.
    features = collection(
        geo_feature(None, {"where": "second"}, [[26.5, 38.5], [26.6, 38.6]]),
        geo_feature(None, {"where": "first"}, [[26.9, 39.0], [27.0, 39.1]]),
    )

    merged, _ = merge_attributes(volatile, features)
    first, second = placemarks(merged)
    assert attributes(first) == {"where": "first"}
    assert attributes(second) == {"where": "second"}


def test_coordinate_formatting_differences_do_not_break_the_match() -> None:
    """The services print the same position differently.

    One writes 26.9, the other 26.900000000000002; string equality matches
    almost nothing while rounding matches everything.
    """
    volatile = STYLED_KML.replace(b"LAYER.1", b"x.fid-1").replace(b"LAYER.2", b"x.fid-2")
    features = collection(
        geo_feature(
            None,
            {"a": "1"},
            [[26.900000000000002, 38.999999999999996], [27.000000000000004, 39.1]],
        )
    )

    _, report = merge_attributes(volatile, features)
    assert report.matched == 1


def test_identifiers_are_preferred_when_they_work() -> None:
    """Geometry is the fallback, not the default: ids are exact and cheap."""
    features = collection(
        geo_feature("LAYER.1", {"via": "id"}, [[99.0, 9.0]]),  # geometry deliberately wrong
        geo_feature("LAYER.2", {"via": "id2"}, [[98.0, 8.0]]),
    )
    merged, report = merge_attributes(STYLED_KML, features)

    assert report.strategy == "id"
    assert attributes(placemarks(merged)[0]) == {"via": "id"}


def test_features_at_identical_coordinates_are_left_unmatched() -> None:
    """Two features in the same place cannot be told apart, so neither is used."""
    volatile = STYLED_KML.replace(b"LAYER.1", b"x.fid-1").replace(b"LAYER.2", b"x.fid-2")
    features = collection(
        geo_feature(None, {"which": "a"}, [[26.9, 39.0], [27.0, 39.1]]),
        geo_feature(None, {"which": "b"}, [[26.9, 39.0], [27.0, 39.1]]),
        geo_feature(None, {"which": "c"}, [[26.5, 38.5], [26.6, 38.6]]),
    )

    merged, report = merge_attributes(volatile, features)
    assert report.matched == 1
    assert attributes(placemarks(merged)[0]) == {}
    assert attributes(placemarks(merged)[1]) == {"which": "c"}


BALLOON_KML = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="{KML_NAMESPACE}"><Document>
  <Placemark id="DRYGEO2.fid-31725dee_585c">
    <description>&lt;h4&gt;DRYGEO2&lt;/h4&gt;
&lt;ul class="textattributes"&gt;
  &lt;li&gt;&lt;strong&gt;&lt;span class="atr-name"&gt;FAY_ADI&lt;/span&gt;:&lt;/strong&gt;
  &lt;span class="atr-value"&gt;ÇEŞME FAYI&lt;/span&gt;&lt;/li&gt;
  &lt;li&gt;&lt;strong&gt;&lt;span class="atr-name"&gt;SEGMENT_ADI&lt;/span&gt;:&lt;/strong&gt;
  &lt;span class="atr-value"&gt;&lt;Null&gt;&lt;/span&gt;&lt;/li&gt;
  &lt;li&gt;&lt;strong&gt;&lt;span class="atr-name"&gt;faytipi&lt;/span&gt;:&lt;/strong&gt;
  &lt;span class="atr-value"&gt;4&lt;/span&gt;&lt;/li&gt;
&lt;/ul&gt;</description>
    <Style><LineStyle><color>4c999999</color><width>3</width></LineStyle></Style>
    <LineString><coordinates>26.9,39.0 27.0,39.1</coordinates></LineString>
  </Placemark>
</Document></kml>
""".encode()


def test_attributes_are_recovered_from_the_description_balloon() -> None:
    """A server with WFS switched off still yields structured attributes.

    MTA's GeoServer answers a WFS capabilities request with ServiceUnavailable,
    so there is no second response to join against -- but WMS has already
    rendered the attributes into an HTML balloon, and they can be promoted from
    there without another request.
    """
    merged, report = attributes_from_descriptions(BALLOON_KML)

    assert report.strategy == "description"
    assert report.matched == 1
    assert report.complete is True

    assert attributes(placemarks(merged)[0]) == {
        "FAY_ADI": "ÇEŞME FAYI",
        "SEGMENT_ADI": None,  # <Null> becomes an empty value
        "faytipi": "4",
    }


def test_promoting_the_balloon_leaves_the_styling_alone() -> None:
    """Promotion must not disturb what made the KML render."""
    merged, _ = attributes_from_descriptions(BALLOON_KML)
    text = merged.decode()

    assert text.count("<LineStyle") == 1
    assert "4c999999" in text
    assert "26.9,39.0 27.0,39.1" in text


def test_the_description_itself_is_kept() -> None:
    """The human-readable balloon still works in a viewer afterwards."""
    merged, _ = attributes_from_descriptions(BALLOON_KML)
    assert b"textattributes" in merged


def test_a_kml_without_balloons_says_so() -> None:
    """A service that renders no attributes gets a specific message."""
    with pytest.raises(ExportError, match="carries an attribute balloon"):
        attributes_from_descriptions(STYLED_KML)


def test_nothing_matching_either_way_explains_both_attempts() -> None:
    """When both strategies fail the message says so."""
    features = collection(geo_feature("OTHER.9", {"a": 1}, [[1.0, 1.0]]))
    with pytest.raises(ExportError, match="by identifier or by coordinates"):
        merge_attributes(STYLED_KML, features)


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
