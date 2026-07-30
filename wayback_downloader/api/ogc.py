"""Generic OGC web-service clients: WMS and WMTS.

Wayback is one specific WMTS deployment. This module talks to *any* compliant
service, which is what makes the same downloader usable against national
orthophoto archives, municipal aerial imagery and the like.

Two protocols, two very different shapes:

* **WMS** renders a map on demand. One ``GetMap`` request returns exactly the
  bounding box and pixel size asked for, so no tiling is involved -- but large
  images are split anyway, because servers commonly cap ``GetMap`` dimensions.
* **WMTS** serves a fixed pyramid of pre-rendered tiles. Capabilities must be
  parsed to learn the layer's tile matrix set, format and URL template. When
  that matrix set is the Web Mercator one (``GoogleMapsCompatible`` and its many
  aliases), the tile addressing is identical to Wayback's and the existing
  projection and stitching code applies unchanged.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from wayback_downloader.exceptions import EndpointDiscoveryError, ValidationError
from wayback_downloader.models import BoundingBox, TileIndex
from wayback_downloader.utils.http import AsyncHttpClient
from wayback_downloader.utils.logger import get_logger

logger = get_logger(__name__)

# Capabilities documents are matched on local element names, ignoring XML
# namespaces entirely. Binding the standard URIs looks more correct but fails
# against real deployments: Esri's Wayback WMTS declares
# `xmlns="https://www.opengis.net/wmts/1.0"` with an https scheme, where the
# OGC standard specifies http, and a URI-keyed lookup silently matches nothing.
# Local names are unambiguous here and survive that kind of drift.

# Tile matrix set identifiers that are the standard Web Mercator pyramid under
# different names. Recognising these lets a WMTS layer reuse the existing XYZ
# tile maths instead of a generic (and much slower) matrix walk.
WEB_MERCATOR_MATRIX_SETS = {
    "googlemapscompatible",
    "google_maps_compatible",
    "webmercatorquad",
    "web_mercator_quad",
    "epsg:3857",
    "epsg:900913",
    "gm",
    "g",
    "default028mm",
}

MAX_WMS_PIXELS = 4096


@dataclass(frozen=True)
class WmtsLayer:
    """One layer advertised by a WMTS service."""

    identifier: str
    title: str
    formats: tuple[str, ...]
    tile_matrix_sets: tuple[str, ...]
    template: str | None
    styles: tuple[str, ...] = ()

    @property
    def default_format(self) -> str:
        """Pick the most useful advertised image format."""
        for preferred in ("image/jpeg", "image/png", "image/webp"):
            if preferred in self.formats:
                return preferred
        return self.formats[0] if self.formats else "image/png"

    @property
    def default_style(self) -> str:
        """Return the style to request when the caller does not care."""
        return self.styles[0] if self.styles else "default"

    def web_mercator_matrix_set(self) -> str | None:
        """Return this layer's Web Mercator matrix set, if it advertises one."""
        for name in self.tile_matrix_sets:
            if name.strip().lower() in WEB_MERCATOR_MATRIX_SETS:
                return name
        return None


@dataclass
class WmtsCapabilities:
    """The parts of a WMTS capabilities document this downloader needs."""

    service_url: str
    title: str
    layers: dict[str, WmtsLayer] = field(default_factory=dict)

    def layer(self, identifier: str | None = None) -> WmtsLayer:
        """Return a layer by identifier, or the only one when unambiguous."""
        if identifier is None:
            if len(self.layers) == 1:
                return next(iter(self.layers.values()))
            raise ValidationError(
                f"This service publishes {len(self.layers)} layers; pass --layer. "
                f"Available: {', '.join(sorted(self.layers)[:12])}"
                + (" ..." if len(self.layers) > 12 else "")
            )
        if identifier in self.layers:
            return self.layers[identifier]

        lowered = {name.lower(): name for name in self.layers}
        if identifier.lower() in lowered:
            return self.layers[lowered[identifier.lower()]]

        raise ValidationError(
            f"Layer {identifier!r} is not published by this service. "
            f"Available: {', '.join(sorted(self.layers)[:12])}"
            + (" ..." if len(self.layers) > 12 else "")
        )


def _local(tag: str) -> str:
    """Return an element tag without its namespace."""
    return tag.rsplit("}", 1)[-1]


def _text(element: ET.Element | None) -> str:
    """Return an element's stripped text, or an empty string."""
    return (element.text or "").strip() if element is not None else ""


def _child(parent: ET.Element | None, name: str) -> ET.Element | None:
    """Return the first direct child with the given local name."""
    if parent is None:
        return None
    for element in parent:
        if _local(element.tag) == name:
            return element
    return None


def _children(parent: ET.Element | None, name: str) -> list[ET.Element]:
    """Return every direct child with the given local name."""
    if parent is None:
        return []
    return [element for element in parent if _local(element.tag) == name]


def _descendant(root: ET.Element, name: str) -> ET.Element | None:
    """Return the first descendant with the given local name."""
    for element in root.iter():
        if _local(element.tag) == name:
            return element
    return None


def parse_wmts_capabilities(xml: str, service_url: str) -> WmtsCapabilities:
    """Parse a WMTS ``GetCapabilities`` document.

    Only the parts needed to build tile URLs are extracted. A layer missing a
    ``ResourceURL`` template is still usable through the KVP interface, so it is
    kept rather than discarded.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise EndpointDiscoveryError(
            f"Could not parse WMTS capabilities from {service_url}: {exc}"
        ) from exc

    title = _text(_child(_descendant(root, "ServiceIdentification"), "Title")) or "WMTS service"
    capabilities = WmtsCapabilities(service_url=service_url, title=title)

    for node in _children(_descendant(root, "Contents"), "Layer"):
        identifier = _text(_child(node, "Identifier"))
        if not identifier:
            continue

        formats = tuple(_text(f) for f in _children(node, "Format") if _text(f))
        matrix_sets = tuple(
            _text(_child(link, "TileMatrixSet"))
            for link in _children(node, "TileMatrixSetLink")
            if _text(_child(link, "TileMatrixSet"))
        )
        styles = tuple(
            _text(_child(style, "Identifier"))
            for style in _children(node, "Style")
            if _text(_child(style, "Identifier"))
        )

        template = None
        for resource in _children(node, "ResourceURL"):
            if resource.get("resourceType") == "tile":
                template = resource.get("template")
                break

        capabilities.layers[identifier] = WmtsLayer(
            identifier=identifier,
            title=_text(_child(node, "Title")) or identifier,
            formats=formats,
            tile_matrix_sets=matrix_sets,
            template=template,
            styles=styles,
        )

    if not capabilities.layers:
        raise EndpointDiscoveryError(f"The WMTS capabilities at {service_url} advertise no layers.")
    return capabilities


def _with_query(url: str, params: dict[str, Any]) -> str:
    """Merge query parameters into a URL, preserving any already present."""
    parts = urlparse(url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))
    existing.update({key: str(value) for key, value in params.items()})
    return urlunparse(parts._replace(query=urlencode(existing)))


def wmts_tile_url(
    capabilities: WmtsCapabilities,
    layer: WmtsLayer,
    matrix_set: str,
    tile: TileIndex,
    image_format: str | None = None,
    style: str | None = None,
) -> str:
    """Build the URL for one WMTS tile.

    Prefers the layer's RESTful ``ResourceURL`` template and falls back to the
    key/value-pair interface, which every WMTS service must support.
    """
    chosen_format = image_format or layer.default_format
    chosen_style = style or layer.default_style

    if layer.template:
        return (
            layer.template.replace("{TileMatrixSet}", matrix_set)
            .replace("{TileMatrix}", str(tile.z))
            .replace("{TileRow}", str(tile.y))
            .replace("{TileCol}", str(tile.x))
            .replace("{Style}", chosen_style)
            .replace("{style}", chosen_style)
            .replace("{Layer}", layer.identifier)
        )

    return _with_query(
        capabilities.service_url,
        {
            "SERVICE": "WMTS",
            "REQUEST": "GetTile",
            "VERSION": "1.0.0",
            "LAYER": layer.identifier,
            "STYLE": chosen_style,
            "FORMAT": chosen_format,
            "TILEMATRIXSET": matrix_set,
            "TILEMATRIX": str(tile.z),
            "TILEROW": str(tile.y),
            "TILECOL": str(tile.x),
        },
    )


def wms_getmap_url(
    service_url: str,
    layers: str,
    bbox: BoundingBox,
    width: int,
    height: int,
    version: str = "1.3.0",
    image_format: str = "image/jpeg",
    styles: str = "",
    transparent: bool = False,
    extra: dict[str, Any] | None = None,
) -> str:
    """Build a WMS ``GetMap`` URL for a WGS84 bounding box.

    WMS 1.3.0 changed the axis order for geographic CRSs: EPSG:4326 became
    latitude-first, whereas 1.1.1 kept longitude-first. Getting this wrong
    silently returns imagery from the wrong place, so the two versions are
    built separately.
    """
    if width <= 0 or height <= 0:
        raise ValidationError("WMS image dimensions must be positive.")
    if width > MAX_WMS_PIXELS or height > MAX_WMS_PIXELS:
        raise ValidationError(
            f"WMS request of {width}x{height} exceeds the {MAX_WMS_PIXELS} pixel "
            "per-side limit; most servers refuse requests this large."
        )

    if version == "1.3.0":
        params: dict[str, Any] = {
            "CRS": "EPSG:4326",
            "BBOX": f"{bbox.south},{bbox.west},{bbox.north},{bbox.east}",
        }
    else:
        params = {
            "SRS": "EPSG:4326",
            "BBOX": f"{bbox.west},{bbox.south},{bbox.east},{bbox.north}",
        }

    params.update(
        {
            "SERVICE": "WMS",
            "REQUEST": "GetMap",
            "VERSION": version,
            "LAYERS": layers,
            "STYLES": styles,
            "FORMAT": image_format,
            "WIDTH": width,
            "HEIGHT": height,
            "TRANSPARENT": "TRUE" if transparent else "FALSE",
        }
    )
    if extra:
        params.update(extra)
    return _with_query(service_url, params)


class OgcClient:
    """Fetches capabilities and imagery from arbitrary WMS/WMTS services."""

    def __init__(self, http: AsyncHttpClient) -> None:
        """Wire the client to the shared HTTP transport."""
        self._http = http

    async def wmts_capabilities(self, service_url: str) -> WmtsCapabilities:
        """Fetch and parse a WMTS capabilities document.

        Accepts either a bare service endpoint or a full ``GetCapabilities``
        URL; the required parameters are added when missing.
        """
        url = service_url
        if "request=" not in url.lower():
            url = _with_query(
                url, {"SERVICE": "WMTS", "REQUEST": "GetCapabilities", "VERSION": "1.0.0"}
            )

        response = await self._http.get(
            url, accept="text/xml,application/xml,*/*", description="WMTS capabilities"
        )
        return parse_wmts_capabilities(response.text, service_url)

    async def fetch_image(self, url: str, description: str) -> bytes:
        """Fetch an image, rejecting an XML service exception dressed as a tile.

        OGC servers report errors as XML with a 200 status, so a response that
        is not actually an image has to be caught here or it would be pasted
        into the mosaic as a corrupt tile.
        """
        response = await self._http.get(url, accept="image/*,*/*", description=description)
        content_type = response.headers.get("Content-Type", "")
        if "xml" in content_type.lower() or response.content[:5] == b"<?xml":
            snippet = response.text[:300].replace("\n", " ")
            raise EndpointDiscoveryError(f"{description} returned a service exception: {snippet}")
        if not response.content:
            raise EndpointDiscoveryError(f"{description} returned an empty body")
        return response.content
