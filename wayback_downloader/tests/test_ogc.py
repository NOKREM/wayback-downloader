"""Tests for the generic WMS and WMTS clients."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from wayback_downloader.api.ogc import (
    MAX_WMS_PIXELS,
    TileMatrixSetDef,
    WmsLayer,
    WmtsCapabilities,
    WmtsLayer,
    is_raster_format,
    normalize_image_format,
    parse_wms_capabilities,
    parse_wmts_capabilities,
    resolve_format,
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


# WMS nests layers: the outer one is a grouping node with no Name, and the
# children inherit its CRS list. Shaped after a real GeoServer response.
WMS_130 = """<?xml version="1.0"?>
<WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms">
  <Service><Name>WMS</Name><Title>GeoServer Web Map Service</Title></Service>
  <Capability>
    <Request>
      <GetMap>
        <Format>image/png</Format>
        <Format>image/jpeg</Format>
        <Format>text/html; subtype=openlayers</Format>
      </GetMap>
    </Request>
    <Layer>
      <Title>Grouping node</Title>
      <CRS>EPSG:4326</CRS>
      <CRS>EPSG:3857</CRS>
      <Layer queryable="1">
        <Name>DRYGEO2</Name>
        <Title>Active faults</Title>
        <EX_GeographicBoundingBox>
          <westBoundLongitude>25.829</westBoundLongitude>
          <eastBoundLongitude>44.809</eastBoundLongitude>
          <southBoundLatitude>35.875</southBoundLatitude>
          <northBoundLatitude>41.867</northBoundLatitude>
        </EX_GeographicBoundingBox>
      </Layer>
      <Layer>
        <Name>KFAY</Name>
        <Title>Other</Title>
        <CRS>EPSG:27700</CRS>
      </Layer>
    </Layer>
  </Capability>
</WMS_Capabilities>
"""

WMS_111 = """<?xml version="1.0"?>
<WMT_MS_Capabilities version="1.1.1">
  <Service><Name>OGC:WMS</Name><Title>Legacy service</Title></Service>
  <Capability>
    <Layer>
      <Title>Root</Title>
      <SRS>EPSG:4326</SRS>
      <Layer>
        <Name>ortho</Name>
        <Title>Orthophoto</Title>
        <LatLonBoundingBox minx="26.0" miny="38.0" maxx="27.0" maxy="39.0"/>
      </Layer>
    </Layer>
  </Capability>
</WMT_MS_Capabilities>
"""


def test_wms_parses_named_layers_only() -> None:
    """Grouping nodes without a Name are not requestable and are skipped."""
    parsed = parse_wms_capabilities(WMS_130, SERVICE)
    assert parsed.title == "GeoServer Web Map Service"
    assert parsed.version == "1.3.0"
    assert [layer.name for layer in parsed.layers] == ["DRYGEO2", "KFAY"]


def test_wms_children_inherit_parent_crs() -> None:
    """A nested layer is requestable in its ancestors' CRSs too."""
    parsed = parse_wms_capabilities(WMS_130, SERVICE)
    assert set(parsed.layer("DRYGEO2").crs) == {"EPSG:4326", "EPSG:3857"}
    # KFAY adds its own on top of the inherited pair.
    assert set(parsed.layer("KFAY").crs) == {"EPSG:4326", "EPSG:3857", "EPSG:27700"}


def test_wms_reads_geographic_bounds() -> None:
    """The 1.3.0 EX_GeographicBoundingBox is parsed into an extent."""
    bounds = parse_wms_capabilities(WMS_130, SERVICE).layer("DRYGEO2").bounds
    assert bounds is not None
    assert bounds.west == pytest.approx(25.829)
    assert bounds.north == pytest.approx(41.867)


def test_wms_111_uses_srs_and_latlonbbox() -> None:
    """The 1.1.1 spellings of CRS and bounds are handled too."""
    parsed = parse_wms_capabilities(WMS_111, SERVICE)
    assert parsed.version == "1.1.1"

    layer = parsed.layer("ortho")
    assert layer.crs == ("EPSG:4326",)
    assert layer.bounds is not None
    assert layer.bounds.east == pytest.approx(27.0)


def test_wms_layer_lookup_is_case_insensitive_and_explains_misses() -> None:
    """A wrong name reports what the service does publish."""
    parsed = parse_wms_capabilities(WMS_130, SERVICE)
    assert parsed.layer("drygeo2").name == "DRYGEO2"
    with pytest.raises(ValidationError, match="DRYGEO2"):
        parsed.layer("missing")


def test_wms_queryable_flag_is_read() -> None:
    """The queryable attribute survives parsing."""
    parsed = parse_wms_capabilities(WMS_130, SERVICE)
    assert parsed.layer("DRYGEO2").queryable is True
    assert parsed.layer("KFAY").queryable is False


def test_wms_capabilities_without_named_layers_are_rejected() -> None:
    """A document with only grouping nodes is an error."""
    document = """<?xml version="1.0"?>
    <WMS_Capabilities version="1.3.0"><Service><Title>x</Title></Service>
    <Capability><Layer><Title>only a group</Title></Layer></Capability></WMS_Capabilities>"""
    with pytest.raises(EndpointDiscoveryError, match="no named layers"):
        parse_wms_capabilities(document, SERVICE)


def test_wms_supports_wgs84_flags_usable_layers() -> None:
    """Layers are marked by whether this tool can request their CRS."""
    parsed = parse_wms_capabilities(WMS_130, SERVICE)
    assert parsed.layer("DRYGEO2").supports_wgs84() is True

    unusable = WmsLayer("x", "x", ("EPSG:27700",), None)
    assert unusable.supports_wgs84() is False


def test_wms_reads_service_level_getmap_formats() -> None:
    """WMS advertises its formats once, under Request/GetMap."""
    parsed = parse_wms_capabilities(WMS_130, SERVICE)
    assert parsed.formats == ("image/png", "image/jpeg", "text/html; subtype=openlayers")
    assert parsed.default_format == "image/png"


def test_wms_without_advertised_formats_still_has_a_default() -> None:
    """A service that advertises nothing falls back to PNG."""
    parsed = parse_wms_capabilities(WMS_111, SERVICE)
    assert parsed.formats == ()
    assert parsed.default_format == "image/png"


@pytest.mark.parametrize(
    ("shorthand", "mime"),
    [
        ("png", "image/png"),
        ("jpg", "image/jpeg"),
        ("jpeg", "image/jpeg"),
        ("webp", "image/webp"),
        ("tif", "image/tiff"),
        ("PNG", "image/png"),
    ],
)
def test_format_shorthands_expand(shorthand: str, mime: str) -> None:
    """Callers can type `png` rather than `image/png`."""
    assert normalize_image_format(shorthand) == mime


def test_full_mime_types_pass_through() -> None:
    """Anything with a slash is left alone, including server-specific types."""
    assert normalize_image_format("image/png; mode=8bit") == "image/png; mode=8bit"
    assert normalize_image_format("image/vnd.jpeg-png") == "image/vnd.jpeg-png"


def test_resolve_format_matches_the_advertised_spelling() -> None:
    """The value sent is the one the service published, not the shorthand."""
    assert resolve_format("jpg", ("image/png", "image/jpeg"), "image/png") == "image/jpeg"
    assert resolve_format(None, ("image/png", "image/jpeg"), "image/png") == "image/png"


def test_resolve_format_rejects_what_is_not_offered() -> None:
    """An unsupported format fails before any request, naming the options.

    Left to the server this arrives as an XML exception carrying HTTP 200,
    which would otherwise be pasted into the mosaic as a corrupt tile.
    """
    with pytest.raises(ValidationError, match="image/png, image/jpeg"):
        resolve_format("image/bogus", ("image/png", "image/jpeg"), "image/png")


def test_resolve_format_trusts_the_caller_when_nothing_is_advertised() -> None:
    """With no advertised list there is nothing to validate against."""
    assert resolve_format("webp", (), "image/png") == "image/webp"


@pytest.mark.parametrize(
    "mime",
    [
        "image/png",
        "image/jpeg",
        "image/png; mode=8bit",
        "image/tiff",
        "image/vnd.jpeg-png",
        "IMAGE/PNG",
    ],
)
def test_raster_formats_are_recognised(mime: str) -> None:
    """Anything the decoder can turn into pixels counts as raster."""
    assert is_raster_format(mime) is True


@pytest.mark.parametrize(
    "mime",
    [
        "application/vnd.google-earth.kml+xml",
        "application/vnd.google-earth.kmz",
        "text/html; subtype=openlayers",
        "image/svg+xml",
    ],
)
def test_non_raster_formats_are_recognised(mime: str) -> None:
    """KML, KMZ and HTML viewers are valid GetMap outputs but not images.

    SVG is excluded despite its `image/` prefix: it is vector XML, and the
    decoder cannot rasterise it.
    """
    assert is_raster_format(mime) is False


def test_advertised_but_unusable_format_is_refused_with_the_alternatives() -> None:
    """A format the service offers but this tool cannot mosaic is rejected.

    Services publish KML and HTML viewers from the same GetMap operation as
    their images, so it is offered, valid, and still useless here.
    """
    advertised = (
        "image/png",
        "image/jpeg",
        "application/vnd.google-earth.kml+xml",
        "text/html; subtype=openlayers",
    )
    with pytest.raises(ValidationError) as excinfo:
        resolve_format("application/vnd.google-earth.kml+xml", advertised, "image/png")

    message = str(excinfo.value)
    assert "not a raster image" in message
    assert "image/png, image/jpeg" in message
    # The alternatives listed must themselves be usable.
    assert "kml" not in message.split("Raster formats offered here:")[1]


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
