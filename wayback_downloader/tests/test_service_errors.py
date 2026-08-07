"""Tests for surfacing what a remote service says when it rejects a request.

OGC servers explain their refusals precisely -- "Unknown TILEMATRIX 16", "tile
dimensions 1024x1024 do not match those of the grid set (256x256)" -- and that
explanation was being discarded in favour of a bare status code, which meant
diagnosing a failure required replaying the request by hand.
"""

from __future__ import annotations

import httpx
import pytest

from wayback_downloader.api.ogc import (
    OgcClient,
    condense_exception_text,
    describe_service_error,
    endpoint_hint,
    suggest_layer,
)
from wayback_downloader.exceptions import ServiceRequestError, ValidationError


def response(body: str, status: int = 400, content_type: str = "text/xml") -> httpx.Response:
    """Build a response carrying an error body."""
    return httpx.Response(
        status,
        text=body,
        headers={"Content-Type": content_type},
        request=httpx.Request("GET", "https://example.org/wmts"),
    )


def test_reads_an_ows_exception_report() -> None:
    """The OGC XML form yields its text and the parameter at fault."""
    body = """<?xml version="1.0" encoding="UTF-8"?>
    <ExceptionReport version="1.1.0" xmlns="http://www.opengis.net/ows/1.1">
      <Exception exceptionCode="InvalidParameterValue" locator="TILEMATRIX">
        <ExceptionText>Unknown TILEMATRIX 16</ExceptionText>
      </Exception>
    </ExceptionReport>"""
    assert describe_service_error(response(body)) == "Unknown TILEMATRIX 16 (TILEMATRIX)"


def test_a_nested_java_exception_is_condensed_to_its_point() -> None:
    """The reason is one sentence buried in three repetitions of a stack chain.

    Verbatim from a GeoServer layer whose style filters on a column the table
    does not have. Everything that matters is the database's own complaint and
    the hint that follows it.
    """
    body = """<?xml version="1.0"?>
    <ServiceExceptionReport version="1.3.0"><ServiceException>
javax.xml.transform.TransformerException: javax.xml.transform.TransformerException: java.lang.RuntimeException
javax.xml.transform.TransformerException: java.lang.RuntimeException
java.lang.RuntimeExceptionorg.postgresql.util.PSQLException: ERROR: column "faytipi" does not exist
  Hint: Perhaps you meant to reference the column "dirifay26.fay_tipi".
  Position: 13
ERROR: column "faytipi" does not exist
  Hint: Perhaps you meant to reference the column "dirifay26.fay_tipi".
  Position: 13
    </ServiceException></ServiceExceptionReport>"""

    described = describe_service_error(response(body))

    assert 'column "faytipi" does not exist' in described
    assert "dirifay26.fay_tipi" in described
    assert "TransformerException" not in described
    assert "java.lang" not in described
    # The same two lines arrive twice; they are worth saying once.
    assert described.count("does not exist") == 1


def test_condensing_leaves_an_ordinary_message_alone() -> None:
    """A message with no Java frames passes through unchanged."""
    assert condense_exception_text("Unknown TILEMATRIX 16") == "Unknown TILEMATRIX 16"


def test_condensing_survives_a_body_that_is_only_frames() -> None:
    """Stripping everything must not leave an empty explanation."""
    assert condense_exception_text("java.lang.RuntimeException") != ""


def test_reads_a_wms_service_exception() -> None:
    """The WMS spelling of an exception report is handled too."""
    body = """<?xml version="1.0"?>
    <ServiceExceptionReport version="1.3.0">
      <ServiceException code="LayerNotDefined">Layer nope does not exist</ServiceException>
    </ServiceExceptionReport>"""
    assert "Layer nope does not exist" in describe_service_error(response(body))


def test_reads_a_geowebcache_html_error() -> None:
    """GeoWebCache renders its reason into an HTML heading, not XML."""
    body = (
        "<html><head><title>GWC Error</title></head><body>"
        "<h4>400: The requested tile dimensions 1024x1024 do not match "
        "those of the grid set (256x256)</h4></body></html>"
    )
    described = describe_service_error(response(body, content_type="text/html"))
    assert "do not match those of the grid set (256x256)" in described


def test_falls_back_to_stripped_text() -> None:
    """An unstructured body still yields something readable."""
    described = describe_service_error(response("plain failure text", content_type="text/plain"))
    assert described == "plain failure text"


def test_empty_body_reports_the_status() -> None:
    """With nothing to quote, the status code is all there is to say."""
    assert "500" in describe_service_error(response("", status=500))


def test_malformed_xml_does_not_raise() -> None:
    """A truncated body must not turn into a second failure."""
    assert describe_service_error(response("<?xml version='1.0'?><broken")) != ""


class _StubHttp:
    """An HTTP transport returning one canned response."""

    def __init__(self, body: str | bytes, content_type: str) -> None:
        """Seed the response body and content type."""
        self._body = body
        self._content_type = content_type

    async def get(self, url: str, **_kwargs: object) -> httpx.Response:
        """Return the canned response."""
        kwargs = {"content": self._body} if isinstance(self._body, bytes) else {"text": self._body}
        return httpx.Response(
            200,
            headers={"Content-Type": self._content_type},
            request=httpx.Request("GET", url),
            **kwargs,
        )


KML_BODY = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><LookAt>'
    "<longitude>35</longitude></LookAt></Document></kml>"
)


async def test_kml_is_returned_not_rejected() -> None:
    """A KML body is a successful response and is handed back untouched.

    KML is XML, so treating every XML body as a service exception reported a
    perfectly valid document as a failure and quoted the KML itself as the
    "error message".
    """
    client = OgcClient(_StubHttp(KML_BODY, "application/vnd.google-earth.kml+xml"))  # type: ignore[arg-type]

    payload = await client.fetch_image("https://example.org/wms", "GetMap", expect_raster=False)
    assert payload.decode().startswith("<?xml")


async def test_non_image_reply_to_a_raster_request_is_rejected() -> None:
    """Asking for a raster and receiving KML means the service ignored us.

    Those bytes would fail deep inside the decoder, so the mismatch is caught
    at the boundary instead.
    """
    client = OgcClient(_StubHttp(KML_BODY, "application/vnd.google-earth.kml+xml"))  # type: ignore[arg-type]

    with pytest.raises(ValidationError, match="raster image was requested"):
        await client.fetch_image("https://example.org/wms", "GetMap", expect_raster=True)


async def test_an_actual_exception_document_is_still_detected() -> None:
    """The narrower check must not stop catching real exception reports."""
    body = (
        '<?xml version="1.0"?><ServiceExceptionReport version="1.3.0">'
        "<ServiceException>Layer nope does not exist</ServiceException>"
        "</ServiceExceptionReport>"
    )
    client = OgcClient(_StubHttp(body, "text/xml"))  # type: ignore[arg-type]

    with pytest.raises(ServiceRequestError, match="Layer nope does not exist"):
        await client.fetch_image("https://example.org/wms", "GetMap 0,0")


async def test_a_real_image_passes_through() -> None:
    """The guards must not reject an ordinary image response."""
    png = bytes.fromhex("89504e470d0a1a0a") + b"rest of the file"
    client = OgcClient(_StubHttp(png, "image/png"))  # type: ignore[arg-type]

    assert await client.fetch_image("https://example.org/wms", "GetMap 0,0") == png


def test_geowebcache_wms_endpoint_gets_a_hint() -> None:
    """The WMS-C endpoint is a common mistake and worth naming.

    It only serves grid-aligned 256x256 requests, so an ordinary bounding box
    fails there while working against the plain WMS endpoint.
    """
    hint = endpoint_hint("https://example.org/geoserver/gwc/service/wms?REQUEST=GetMap")
    assert "WMS-C" in hint
    assert "wmts" in hint


def test_ordinary_endpoints_get_no_hint() -> None:
    """The hint must not fire on a normal WMS endpoint."""
    assert endpoint_hint("https://example.org/geoserver/mta/wms") == ""
    assert endpoint_hint("https://example.org/geoserver/gwc/service/wmts") == ""


def test_suggests_the_name_without_a_workspace_prefix() -> None:
    """A workspace-scoped endpoint drops the prefix the tile cache keeps."""
    assert "DRYGEO2" in suggest_layer("mta:DRYGEO2", ["DAF2", "DRYGEO2", "KFAY"])


def test_suggests_the_name_with_a_workspace_prefix() -> None:
    """The reverse direction matters just as much."""
    assert "mta:DRYGEO2" in suggest_layer("DRYGEO2", ["mta:DAF2", "mta:DRYGEO2"])


@pytest.mark.parametrize("wanted", ["totally_absent", "DRYGEO3"])
def test_no_suggestion_when_nothing_is_close(wanted: str) -> None:
    """A genuinely unknown name gets no misleading suggestion."""
    assert suggest_layer(wanted, ["DAF2", "DRYGEO2"]) == ""
