"""Tests for the generic WMS and WMTS clients."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from wayback_downloader.api.ogc import (
    MAX_WMS_PIXELS,
    TileMatrixSetDef,
    WmtsCapabilities,
    WmtsLayer,
    parse_wmts_capabilities,
    wms_getmap_url,
    wmts_tile_url,
)
from wayback_downloader.exceptions import EndpointDiscoveryError, ValidationError
from wayback_downloader.models import BoundingBox, TileIndex

SERVICE = "https://example.org/wmts"

# Shaped like a real capabilities document, including the RESTful ResourceURL
# template and the KVP-only second layer.
CAPABILITIES = """<?xml version="1.0" encoding="UTF-8"?>
<Capabilities xmlns="http://www.opengis.net/wmts/1.0"
              xmlns:ows="http://www.opengis.net/ows/1.1">
  <ows:ServiceIdentification>
    <ows:Title>Example Orthophoto Service</ows:Title>
  </ows:ServiceIdentification>
  <Contents>
    <Layer>
      <ows:Title>Aerial 2024</ows:Title>
      <ows:Identifier>aerial_2024</ows:Identifier>
      <Style><ows:Identifier>default</ows:Identifier></Style>
      <Format>image/jpeg</Format>
      <Format>image/png</Format>
      <TileMatrixSetLink><TileMatrixSet>GoogleMapsCompatible</TileMatrixSet></TileMatrixSetLink>
      <ResourceURL format="image/jpeg" resourceType="tile"
        template="https://example.org/t/{TileMatrixSet}/{TileMatrix}/{TileCol}/{TileRow}.jpg"/>
    </Layer>
    <Layer>
      <ows:Title>National Grid Layer</ows:Title>
      <ows:Identifier>national_grid</ows:Identifier>
      <Format>image/png</Format>
      <TileMatrixSetLink><TileMatrixSet>EPSG:27700</TileMatrixSet></TileMatrixSetLink>
    </Layer>
  </Contents>
</Capabilities>
"""


def caps() -> WmtsCapabilities:
    """Parse the sample capabilities document."""
    return parse_wmts_capabilities(CAPABILITIES, SERVICE)


def test_parses_layers_and_their_attributes() -> None:
    """Identifier, title, formats, matrix sets and template all come through."""
    parsed = caps()
    assert parsed.title == "Example Orthophoto Service"
    assert set(parsed.layers) == {"aerial_2024", "national_grid"}

    aerial = parsed.layers["aerial_2024"]
    assert aerial.title == "Aerial 2024"
    assert aerial.formats == ("image/jpeg", "image/png")
    assert aerial.tile_matrix_sets == ("GoogleMapsCompatible",)
    assert aerial.styles == ("default",)
    assert aerial.template is not None


def test_recognises_web_mercator_under_its_aliases() -> None:
    """The Web Mercator pyramid is spelled many ways across services."""
    assert caps().layers["aerial_2024"].web_mercator_matrix_set() == "GoogleMapsCompatible"

    for alias in ("WebMercatorQuad", "EPSG:3857", "google_maps_compatible", "GM", "epsg:900913"):
        layer = WmtsLayer("x", "x", ("image/png",), (alias,), None)
        assert layer.web_mercator_matrix_set() == alias, alias


def test_non_mercator_layer_is_reported_not_guessed() -> None:
    """A layer in another projection must not be silently mis-tiled."""
    assert caps().layers["national_grid"].web_mercator_matrix_set() is None


def test_single_layer_service_needs_no_identifier() -> None:
    """With one layer the caller can omit --layer."""
    parsed = caps()
    del parsed.layers["national_grid"]
    assert parsed.layer().identifier == "aerial_2024"


def test_ambiguous_layer_choice_is_refused() -> None:
    """With several layers an explicit choice is required."""
    with pytest.raises(ValidationError, match="pass --layer"):
        caps().layer()


def test_unknown_layer_lists_what_exists() -> None:
    """A wrong identifier reports the available ones."""
    with pytest.raises(ValidationError, match="aerial_2024"):
        caps().layer("nope")


def test_layer_lookup_is_case_insensitive() -> None:
    """Identifiers differing only in case still resolve."""
    assert caps().layer("AERIAL_2024").identifier == "aerial_2024"


def test_parses_https_scheme_namespaces() -> None:
    """Namespace URIs that use https must still parse.

    The OGC standard specifies `http://www.opengis.net/wmts/1.0`, but Esri's
    Wayback WMTS declares the https form. A namespace-URI-keyed parser matches
    nothing against it and reports "no layers" on a perfectly valid document --
    which is exactly what happened before the parser moved to local names.
    """
    document = CAPABILITIES.replace("http://www.opengis.net", "https://www.opengis.net")
    parsed = parse_wmts_capabilities(document, SERVICE)

    assert parsed.title == "Example Orthophoto Service"
    assert set(parsed.layers) == {"aerial_2024", "national_grid"}
    assert parsed.layers["aerial_2024"].web_mercator_matrix_set() == "GoogleMapsCompatible"


def test_parses_documents_using_explicit_prefixes() -> None:
    """A server binding its own prefixes rather than a default namespace works."""
    document = """<?xml version="1.0"?>
    <wmts:Capabilities xmlns:wmts="http://www.opengis.net/wmts/1.0"
                       xmlns:ows="http://www.opengis.net/ows/1.1">
      <ows:ServiceIdentification><ows:Title>Prefixed</ows:Title></ows:ServiceIdentification>
      <wmts:Contents>
        <wmts:Layer>
          <ows:Identifier>only</ows:Identifier>
          <wmts:Format>image/png</wmts:Format>
          <wmts:TileMatrixSetLink>
            <wmts:TileMatrixSet>WebMercatorQuad</wmts:TileMatrixSet>
          </wmts:TileMatrixSetLink>
        </wmts:Layer>
      </wmts:Contents>
    </wmts:Capabilities>"""
    parsed = parse_wmts_capabilities(document, SERVICE)

    assert parsed.title == "Prefixed"
    assert parsed.layer().identifier == "only"
    assert parsed.layer().web_mercator_matrix_set() == "WebMercatorQuad"


def test_malformed_capabilities_are_reported() -> None:
    """Unparseable XML is a discovery error, not a crash."""
    with pytest.raises(EndpointDiscoveryError, match="Could not parse"):
        parse_wmts_capabilities("this is not xml <", SERVICE)


def test_capabilities_without_layers_are_rejected() -> None:
    """A document advertising nothing usable is an error."""
    empty = """<?xml version="1.0"?>
    <Capabilities xmlns="http://www.opengis.net/wmts/1.0"
                  xmlns:ows="http://www.opengis.net/ows/1.1"><Contents/></Capabilities>"""
    with pytest.raises(EndpointDiscoveryError, match="no layers"):
        parse_wmts_capabilities(empty, SERVICE)


def test_rest_template_is_filled_in() -> None:
    """A RESTful ResourceURL template is preferred and substituted."""
    parsed = caps()
    url = wmts_tile_url(
        parsed,
        parsed.layers["aerial_2024"],
        "GoogleMapsCompatible",
        TileIndex(z=14, x=9419, y=6273),
    )
    assert url == "https://example.org/t/GoogleMapsCompatible/14/9419/6273.jpg"


def test_kvp_fallback_when_no_template() -> None:
    """Without a template the mandatory KVP interface is used."""
    parsed = caps()
    url = wmts_tile_url(
        parsed, parsed.layers["national_grid"], "EPSG:27700", TileIndex(z=5, x=3, y=7)
    )
    query = {k.upper(): v[0] for k, v in parse_qs(urlparse(url).query).items()}

    assert query["SERVICE"] == "WMTS"
    assert query["REQUEST"] == "GetTile"
    assert query["LAYER"] == "national_grid"
    assert query["TILEMATRIX"] == "5"
    assert query["TILECOL"] == "3"
    assert query["TILEROW"] == "7"


# Shaped like GeoServer/GeoWebCache, which names its zoom levels after the
# gridset and publishes one RESTful template per image format.
GEOSERVER = """<?xml version="1.0"?>
<Capabilities xmlns="http://www.opengis.net/wmts/1.0"
              xmlns:ows="http://www.opengis.net/ows/1.1">
  <ows:ServiceIdentification><ows:Title>GeoServer WMTS</ows:Title></ows:ServiceIdentification>
  <Contents>
    <Layer>
      <ows:Identifier>mta:DRYGEO2</ows:Identifier>
      <Style><ows:Identifier>DRFAY</ows:Identifier></Style>
      <Format>image/png</Format>
      <Format>image/jpeg</Format>
      <TileMatrixSetLink><TileMatrixSet>EPSG:900913</TileMatrixSet></TileMatrixSetLink>
      <ResourceURL format="image/png" resourceType="tile"
        template="https://s/rest/mta:DRYGEO2/{style}/{TileMatrixSet}/{TileMatrix}/{TileRow}/{TileCol}?format=image/png"/>
      <ResourceURL format="image/jpeg" resourceType="tile"
        template="https://s/rest/mta:DRYGEO2/{style}/{TileMatrixSet}/{TileMatrix}/{TileRow}/{TileCol}?format=image/jpeg"/>
    </Layer>
    <TileMatrixSet>
      <ows:Identifier>EPSG:900913</ows:Identifier>
      <TileMatrix><ows:Identifier>EPSG:900913:0</ows:Identifier></TileMatrix>
      <TileMatrix><ows:Identifier>EPSG:900913:1</ows:Identifier></TileMatrix>
      <TileMatrix><ows:Identifier>EPSG:900913:2</ows:Identifier></TileMatrix>
    </TileMatrixSet>
  </Contents>
</Capabilities>
"""


def geoserver() -> WmtsCapabilities:
    """Parse the GeoServer-shaped capabilities document."""
    return parse_wmts_capabilities(GEOSERVER, SERVICE)


def test_tile_matrix_identifier_is_not_the_bare_zoom() -> None:
    """GeoServer names levels after the gridset, and rejects a bare number.

    Sending `16` where the service published `EPSG:900913:16` returns
    HTTP 400 `InvalidParameterValue: Unknown TILEMATRIX 16` -- observed
    against a live GeoServer deployment.
    """
    parsed = geoserver()
    assert parsed.tile_matrix_id("EPSG:900913", 2) == "EPSG:900913:2"

    url = wmts_tile_url(
        parsed, parsed.layers["mta:DRYGEO2"], "EPSG:900913", TileIndex(z=2, x=3, y=1)
    )
    assert "/EPSG:900913/EPSG:900913:2/1/3" in url


def test_tile_matrix_falls_back_to_the_zoom_number() -> None:
    """An unadvertised matrix set still produces a usable request.

    Esri's Wayback names its levels with the bare number, and a caller may
    override the matrix set by hand.
    """
    assert caps().tile_matrix_id("GoogleMapsCompatible", 14) == "14"
    assert geoserver().tile_matrix_id("EPSG:4326", 5) == "5"


def test_tile_matrix_matches_by_trailing_level_not_position() -> None:
    """Levels are matched on their trailing number, not their order."""
    definition = TileMatrixSetDef("s", ("s:10", "s:11", "s:12"))
    assert definition.matrix_for_zoom(11) == "s:11"
    assert definition.matrix_for_zoom(12) == "s:12"
    assert definition.matrix_for_zoom(99) is None


def test_template_matches_the_requested_format() -> None:
    """With one template per format, the requested format must win.

    Taking the first published template would hand back PNG whenever the
    caller asked for JPEG.
    """
    parsed = geoserver()
    layer = parsed.layers["mta:DRYGEO2"]
    tile = TileIndex(z=1, x=0, y=0)

    assert "format=image/jpeg" in wmts_tile_url(
        parsed, layer, "EPSG:900913", tile, image_format="image/jpeg"
    )
    assert "format=image/png" in wmts_tile_url(
        parsed, layer, "EPSG:900913", tile, image_format="image/png"
    )


def test_lowercase_style_placeholder_is_substituted() -> None:
    """GeoServer templates use `{style}`, not `{Style}`."""
    parsed = geoserver()
    url = wmts_tile_url(
        parsed, parsed.layers["mta:DRYGEO2"], "EPSG:900913", TileIndex(z=1, x=0, y=0)
    )
    assert "{style}" not in url
    assert "/DRFAY/" in url


def test_default_format_prefers_jpeg() -> None:
    """JPEG is chosen over PNG for photographic imagery."""
    assert caps().layers["aerial_2024"].default_format == "image/jpeg"
    assert caps().layers["national_grid"].default_format == "image/png"


BOX = BoundingBox(west=26.965, south=38.795, east=26.980, north=38.805)


def test_wms_130_uses_latitude_first_axis_order() -> None:
    """WMS 1.3.0 made EPSG:4326 latitude-first.

    Getting this backwards returns imagery from a completely different place
    while still returning HTTP 200, so it is worth pinning.
    """
    query = parse_qs(urlparse(wms_getmap_url(SERVICE, "ortho", BOX, 512, 512)).query)
    assert query["CRS"][0] == "EPSG:4326"
    assert query["BBOX"][0] == "38.795,26.965,38.805,26.98"
    assert "SRS" not in query


def test_wms_111_uses_longitude_first_axis_order() -> None:
    """WMS 1.1.1 keeps the longitude-first order and uses SRS, not CRS."""
    url = wms_getmap_url(SERVICE, "ortho", BOX, 512, 512, version="1.1.1")
    query = parse_qs(urlparse(url).query)
    assert query["SRS"][0] == "EPSG:4326"
    assert query["BBOX"][0] == "26.965,38.795,26.98,38.805"
    assert "CRS" not in query


def test_wms_carries_the_requested_parameters() -> None:
    """Layers, size, format and transparency reach the request."""
    url = wms_getmap_url(
        SERVICE, "a,b", BOX, 800, 600, image_format="image/png", transparent=True, styles="s1"
    )
    query = parse_qs(urlparse(url).query)
    assert query["LAYERS"][0] == "a,b"
    assert query["WIDTH"][0] == "800"
    assert query["HEIGHT"][0] == "600"
    assert query["FORMAT"][0] == "image/png"
    assert query["TRANSPARENT"][0] == "TRUE"
    assert query["STYLES"][0] == "s1"


def test_wms_preserves_existing_query_parameters() -> None:
    """A service URL that already carries parameters keeps them.

    Many deployments embed an access path or token in the endpoint itself.
    """
    url = wms_getmap_url("https://example.org/wms?map=/data/x.map", "ortho", BOX, 256, 256)
    query = parse_qs(urlparse(url).query)
    assert query["map"][0] == "/data/x.map"
    assert query["REQUEST"][0] == "GetMap"


def test_wms_rejects_oversized_requests() -> None:
    """A request beyond the per-side cap is refused with the reason."""
    with pytest.raises(ValidationError, match="exceeds"):
        wms_getmap_url(SERVICE, "ortho", BOX, MAX_WMS_PIXELS + 1, 512)


def test_wms_rejects_empty_dimensions() -> None:
    """Zero-sized requests are refused."""
    with pytest.raises(ValidationError, match="positive"):
        wms_getmap_url(SERVICE, "ortho", BOX, 0, 512)
