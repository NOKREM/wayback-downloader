"""Tests for the WFS client."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from wayback_downloader.api.wfs import (
    BBOX_CRS_URN,
    bbox_parameter,
    output_extension,
    parse_wfs_capabilities,
    resolve_output_format,
    short_crs,
    summarize_features,
    wfs_getfeature_url,
)
from wayback_downloader.exceptions import EndpointDiscoveryError, ValidationError
from wayback_downloader.models import BoundingBox

SERVICE = "https://example.org/geoserver/wfs"
BOX = BoundingBox(west=26.0, south=38.0, east=28.0, north=39.5)

# WFS 2.0.0, whose feature types carry ows:WGS84BoundingBox corners.
CAPS_200 = """<?xml version="1.0"?>
<WFS_Capabilities version="2.0.0" xmlns="http://www.opengis.net/wfs/2.0"
                  xmlns:ows="http://www.opengis.net/ows/1.1">
  <ows:ServiceIdentification><ows:Title>GeoServer WFS</ows:Title></ows:ServiceIdentification>
  <ows:OperationsMetadata>
    <ows:Operation name="GetFeature">
      <ows:Parameter name="outputFormat">
        <ows:AllowedValues>
          <ows:Value>application/json</ows:Value>
          <ows:Value>csv</ows:Value>
          <ows:Value>shape-zip</ows:Value>
        </ows:AllowedValues>
      </ows:Parameter>
    </ows:Operation>
  </ows:OperationsMetadata>
  <FeatureTypeList>
    <FeatureType>
      <Name>afad:DFY_GEO_WGS84_2013</Name>
      <Title>Active faults</Title>
      <DefaultCRS>urn:ogc:def:crs:EPSG::4326</DefaultCRS>
      <ows:WGS84BoundingBox>
        <ows:LowerCorner>25.829 35.936</ows:LowerCorner>
        <ows:UpperCorner>44.739 41.867</ows:UpperCorner>
      </ows:WGS84BoundingBox>
    </FeatureType>
  </FeatureTypeList>
</WFS_Capabilities>
"""

# WFS 1.0.0: attribute-style bounds, bare format names, SRS not CRS.
CAPS_100 = """<?xml version="1.0"?>
<WFS_Capabilities version="1.0.0" xmlns="http://www.opengis.net/wfs">
  <Service><Title>Legacy WFS</Title></Service>
  <Capability><Request><GetFeature>
    <ResultFormat><GML2/><JSON/><CSV/><SHAPE-ZIP/><KML/></ResultFormat>
  </GetFeature></Request></Capability>
  <FeatureTypeList>
    <FeatureType>
      <Name>afad:acc_stations</Name>
      <Title>Stations</Title>
      <SRS>EPSG:4326</SRS>
      <LatLongBoundingBox minx="25.9" miny="35.0" maxx="44.3" maxy="42.0"/>
    </FeatureType>
  </FeatureTypeList>
</WFS_Capabilities>
"""


def test_parses_a_2_0_0_document() -> None:
    """Version, title, feature type, CRS and extent all come through."""
    caps = parse_wfs_capabilities(CAPS_200, SERVICE)
    assert caps.version == "2.0.0"
    assert caps.title == "GeoServer WFS"
    assert caps.formats == ("application/json", "csv", "shape-zip")

    feature_type = caps.feature_type("afad:DFY_GEO_WGS84_2013")
    assert feature_type.title == "Active faults"
    assert feature_type.bounds is not None
    assert feature_type.bounds.west == pytest.approx(25.829)
    assert feature_type.bounds.north == pytest.approx(41.867)


def test_parses_a_1_0_0_document() -> None:
    """The legacy spellings of bounds, CRS and formats are handled too."""
    caps = parse_wfs_capabilities(CAPS_100, SERVICE)
    assert caps.version == "1.0.0"
    assert set(caps.formats) == {"GML2", "JSON", "CSV", "SHAPE-ZIP", "KML"}

    feature_type = caps.feature_type("afad:acc_stations")
    assert feature_type.crs == ("EPSG:4326",)
    assert feature_type.bounds is not None
    assert feature_type.bounds.east == pytest.approx(44.3)


def test_capabilities_without_feature_types_are_rejected() -> None:
    """A document advertising nothing usable is an error."""
    empty = """<?xml version="1.0"?><WFS_Capabilities version="2.0.0"
        xmlns="http://www.opengis.net/wfs/2.0"><FeatureTypeList/></WFS_Capabilities>"""
    with pytest.raises(EndpointDiscoveryError, match="no feature types"):
        parse_wfs_capabilities(empty, SERVICE)


def test_unknown_feature_type_suggests_and_lists() -> None:
    """A wrong name reports what exists, and spots a prefix mismatch."""
    caps = parse_wfs_capabilities(CAPS_200, SERVICE)
    with pytest.raises(ValidationError, match="DFY_GEO_WGS84_2013"):
        caps.feature_type("DFY_GEO_WGS84_2013_typo")

    with pytest.raises(ValidationError, match="Did you mean"):
        caps.feature_type("DFY_GEO_WGS84_2013")


def test_version_2_uses_typenames_and_count() -> None:
    """2.0.0 renamed both the type parameter and the limit."""
    query = parse_qs(urlparse(wfs_getfeature_url(SERVICE, "x", "2.0.0", max_features=5)).query)
    assert query["typeNames"][0] == "x"
    assert query["count"][0] == "5"
    assert "typeName" not in query
    assert "maxFeatures" not in query


@pytest.mark.parametrize("version", ["1.1.0", "1.0.0"])
def test_earlier_versions_use_typename_and_maxfeatures(version: str) -> None:
    """1.x keeps the singular parameter and the older limit name."""
    query = parse_qs(urlparse(wfs_getfeature_url(SERVICE, "x", version, max_features=5)).query)
    assert query["typeName"][0] == "x"
    assert query["maxFeatures"][0] == "5"
    assert "typeNames" not in query


def test_bbox_pairs_the_urn_crs_with_latitude_first() -> None:
    """The CRS spelling and the axis order have to agree.

    GeoServer reads the short `EPSG:4326` as longitude-first and this URN as
    latitude-first. Pairing latitude-first coordinates with the short code
    returns zero features and HTTP 200 -- verified against a live service --
    so the URN is what makes the request unambiguous.
    """
    assert bbox_parameter(BOX, "2.0.0") == f"38.0,26.0,39.5,28.0,{BBOX_CRS_URN}"
    assert bbox_parameter(BOX, "1.1.0") == f"38.0,26.0,39.5,28.0,{BBOX_CRS_URN}"


def test_bbox_for_1_0_0_is_longitude_first() -> None:
    """1.0.0 predates the URN convention and is longitude-first."""
    assert bbox_parameter(BOX, "1.0.0") == "26.0,38.0,28.0,39.5,EPSG:4326"


def test_bbox_always_carries_a_crs() -> None:
    """Without a CRS the same four numbers mean different places."""
    for version in ("2.0.0", "1.1.0", "1.0.0"):
        assert bbox_parameter(BOX, version).count(",") == 4


def test_unsupported_version_is_refused() -> None:
    """An unknown version would silently produce the wrong parameter names."""
    with pytest.raises(ValidationError, match="Unsupported WFS version"):
        wfs_getfeature_url(SERVICE, "x", "3.0.0")


def test_filters_and_paging_reach_the_request() -> None:
    """CQL, sorting, paging and property selection are passed through."""
    url = wfs_getfeature_url(
        SERVICE,
        "x",
        "2.0.0",
        cql_filter="FAYTIPI=2",
        sort_by="FAYNO",
        start_index=100,
        property_names="FAYADI,FAYTIPI",
    )
    query = parse_qs(urlparse(url).query)
    assert query["CQL_FILTER"][0] == "FAYTIPI=2"
    assert query["sortBy"][0] == "FAYNO"
    assert query["startIndex"][0] == "100"
    assert query["propertyName"][0] == "FAYADI,FAYTIPI"


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_limit_is_refused(value: int) -> None:
    """A limit of zero or less would silently return nothing."""
    with pytest.raises(ValidationError, match="positive"):
        wfs_getfeature_url(SERVICE, "x", "2.0.0", max_features=value)


def test_format_resolution_crosses_vocabularies() -> None:
    """The same request works against either version's format spelling.

    2.0.0 advertises `application/json`, 1.0.0 advertises `JSON`.
    """
    assert resolve_output_format("geojson", ("application/json", "csv"), "csv") == (
        "application/json"
    )
    assert resolve_output_format("geojson", ("GML2", "JSON", "CSV"), "GML2") == "JSON"
    assert resolve_output_format("shp", ("SHAPE-ZIP", "JSON"), "JSON") == "SHAPE-ZIP"
    assert resolve_output_format("csv", ("text/csv",), "text/csv") == "text/csv"


def test_format_resolution_prefers_an_exact_match() -> None:
    """An exactly advertised spelling wins over a family sibling."""
    assert resolve_output_format("csv", ("csv", "text/csv"), "csv") == "csv"


def test_unknown_format_is_refused_with_the_alternatives() -> None:
    """An unsupported format names what would have worked."""
    with pytest.raises(ValidationError, match="JSON"):
        resolve_output_format("dwg", ("GML2", "JSON"), "JSON")


@pytest.mark.parametrize(
    ("output_format", "extension"),
    [
        ("application/json", "geojson"),
        ("JSON", "geojson"),
        ("csv", "csv"),
        ("shape-zip", "zip"),
        ("KML", "kml"),
        ("GML2", "gml"),
        ("text/xml; subtype=gml/3.2", "gml"),
    ],
)
def test_extension_matches_the_output_format(output_format: str, extension: str) -> None:
    """Each format lands on disk with the extension that opens it."""
    assert output_extension(output_format) == extension


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("EPSG:4326", "EPSG:4326"),
        ("urn:ogc:def:crs:EPSG::4326", "EPSG:4326"),
        ("http://www.opengis.net/def/crs/EPSG/0/3857", "EPSG:3857"),
        ("", "-"),
    ],
)
def test_crs_identifiers_are_shortened(identifier: str, expected: str) -> None:
    """Services name the same system three ways; only the code is useful."""
    assert short_crs(identifier) == expected


def test_geojson_is_summarised() -> None:
    """Feature count, geometry types and attributes are reported."""
    payload = json.dumps(
        {
            "type": "FeatureCollection",
            "numberMatched": 538,
            "features": [
                {
                    "geometry": {"type": "MultiLineString", "coordinates": [[[26.1, 38.2]]]},
                    "properties": {"FAYTIPI": 2, "FAYADI": "x"},
                }
            ],
        }
    ).encode()

    summary = summarize_features(payload, "application/json")
    assert summary["feature_count"] == 1
    assert summary["geometry_types"] == ["MultiLineString"]
    assert summary["properties"] == ["FAYADI", "FAYTIPI"]
    assert summary["numberMatched"] == 538


def test_non_json_output_is_not_guessed_at() -> None:
    """GML and shapefile archives are written through unparsed."""
    summary = summarize_features(b"PK\x03\x04binary", "shape-zip")
    assert summary == {"byte_size": len(b"PK\x03\x04binary")}


def test_malformed_json_does_not_raise() -> None:
    """A truncated response must not turn into a second failure."""
    assert "feature_count" not in summarize_features(b"{not json", "application/json")
