"""Tests for surfacing what a remote service says when it rejects a request.

OGC servers explain their refusals precisely -- "Unknown TILEMATRIX 16", "tile
dimensions 1024x1024 do not match those of the grid set (256x256)" -- and that
explanation was being discarded in favour of a bare status code, which meant
diagnosing a failure required replaying the request by hand.
"""

from __future__ import annotations

import httpx
import pytest

from wayback_downloader.api.ogc import describe_service_error, endpoint_hint, suggest_layer


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
