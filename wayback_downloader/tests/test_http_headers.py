"""Tests for scoping request headers to their target host.

The motivating failure: every request carried an ``Origin`` and ``Referer``
naming Esri's Wayback web app, including requests to unrelated services. AFAD's
GeoServer sits behind a WAF that answers a plain request with 200 and the same
request carrying those headers with 401 -- so the tool reported a public
service as requiring authentication.
"""

from __future__ import annotations

import pytest

from wayback_downloader.config import Settings
from wayback_downloader.utils.http import (
    WAYBACK_APP_ORIGIN,
    build_headers,
    headers_for,
    targets_wayback_app,
)

APP_ONLY = {"Origin", "Referer", "Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site"}


@pytest.fixture
def settings() -> Settings:
    """Default settings."""
    return Settings()


@pytest.mark.parametrize(
    "url",
    [
        "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/x.xml",
        "https://livingatlas.arcgis.com/wayback/",
        "https://services.arcgisonline.com/tile/1/2/3",
        "https://www.esri.com/anything",
    ],
)
def test_esri_hosts_receive_the_app_headers(settings: Settings, url: str) -> None:
    """Requests to the service the headers describe still carry them."""
    assert targets_wayback_app(url) is True
    headers = headers_for(settings, url)
    assert headers["Origin"] == WAYBACK_APP_ORIGIN
    assert APP_ONLY <= set(headers)


@pytest.mark.parametrize(
    "url",
    [
        "https://ivmegeoserver.afad.gov.tr/geoserver/wms",
        "https://mtayenicbs-geoserver.mta.gov.tr/geoserver/gwc/service/wmts",
        "https://example.org/wms",
        "https://notarcgis.com.evil.example/x",
    ],
)
def test_other_hosts_receive_no_app_headers(settings: Settings, url: str) -> None:
    """Third-party services get an honest, neutral header set."""
    assert targets_wayback_app(url) is False
    headers = headers_for(settings, url)
    assert APP_ONLY.isdisjoint(headers)


def test_host_matching_is_not_a_substring_check(settings: Settings) -> None:
    """A host merely containing the suffix must not be treated as Esri's.

    `notarcgis.com` ends with the characters of `arcgis.com` without being a
    subdomain of it.
    """
    assert targets_wayback_app("https://notarcgis.com/x") is False
    assert targets_wayback_app("https://tiles.arcgis.com/x") is True


def test_neutral_headers_are_always_present(settings: Settings) -> None:
    """Identity, language and encoding go to every host."""
    for url in ("https://example.org/wms", "https://tiles.arcgis.com/x"):
        headers = headers_for(settings, url)
        assert headers["User-Agent"] == settings.user_agent
        assert "Accept-Encoding" in headers
        assert "Accept-Language" in headers


def test_accept_is_carried_through(settings: Settings) -> None:
    """The per-request Accept overrides the default."""
    headers = headers_for(settings, "https://example.org/wms", "image/*,*/*")
    assert headers["Accept"] == "image/*,*/*"
    assert headers_for(settings, "https://example.org/wms")["Accept"] == "*/*"


def test_base_headers_carry_no_origin(settings: Settings) -> None:
    """The shared base set must stay neutral, since it reaches every host."""
    assert APP_ONLY.isdisjoint(build_headers(settings))
