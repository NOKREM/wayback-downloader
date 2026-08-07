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

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from wayback_downloader.exceptions import (
    EndpointDiscoveryError,
    ServiceRequestError,
    ValidationError,
)
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

# Short names accepted on the command line, so callers need not type a MIME
# type. Anything containing a slash is passed through untouched.
_FORMAT_ALIASES = {
    "png": "image/png",
    "png8": "image/png; mode=8bit",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "svg": "image/svg+xml",
}


# Formats whose bytes this downloader can actually decode and mosaic. Services
# routinely advertise KML, KMZ, SVG and HTML viewers from the same GetMap
# operation; those are legitimate responses, just not rasters. SVG is excluded
# despite its `image/` prefix because it is vector XML.
_VECTOR_IMAGE_TYPES = {"image/svg+xml", "image/svg"}


def is_raster_format(mime: str) -> bool:
    """Whether a MIME type names a raster image this tool can decode.

    This decides *how* a format is downloaded, not whether it can be: a raster
    is tiled and mosaicked, anything else is fetched in one request and saved
    verbatim.
    """
    base = mime.split(";")[0].strip().lower()
    return base.startswith("image/") and base not in _VECTOR_IMAGE_TYPES


# File extensions for the formats OGC services commonly publish. Anything not
# listed falls back to the MIME subtype, which is right often enough.
_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/tiff": "tif",
    "image/geotiff": "tif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
    "application/vnd.google-earth.kml+xml": "kml",
    "application/vnd.google-earth.kmz": "kmz",
    "application/json": "json",
    "application/vnd.geo+json": "geojson",
    "application/pdf": "pdf",
    "text/html": "html",
    "text/plain": "txt",
    "application/atom+xml": "xml",
}

# Pillow's writer name for each raster type it can encode. A format absent here
# is decodable but not writable, and is re-encoded as PNG.
_PILLOW_WRITERS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/jpg": "JPEG",
    "image/gif": "GIF",
    "image/tiff": "TIFF",
    "image/geotiff": "TIFF",
    "image/webp": "WEBP",
    "image/bmp": "BMP",
}


def format_extension(mime: str) -> str:
    """Return the file extension to use for a MIME type."""
    base = mime.split(";")[0].strip().lower()
    if base in _EXTENSIONS:
        return _EXTENSIONS[base]

    subtype = base.rsplit("/", 1)[-1]
    subtype = subtype.split("+")[0].replace("vnd.", "")
    return re.sub(r"[^a-z0-9]+", "", subtype) or "bin"


def pillow_writer(mime: str) -> str | None:
    """Return Pillow's writer name for a raster type, or None if unsupported."""
    return _PILLOW_WRITERS.get(mime.split(";")[0].strip().lower())


def normalize_image_format(value: str) -> str:
    """Expand a short format name into its MIME type.

    ``png`` becomes ``image/png``; anything already containing a slash is left
    alone so unusual server-specific types still work.
    """
    text = value.strip()
    if "/" in text:
        return text
    return _FORMAT_ALIASES.get(text.lower(), text)


def resolve_format(
    requested: str | None,
    advertised: Sequence[str],
    default: str,
    normalizer: Callable[[str], str] | None = None,
) -> str:
    """Pick the format to request, checking it against what is offered.

    ``normalizer`` expands shorthands and defaults to the image table; WFS
    passes its own, since ``geojson`` and ``shp`` mean nothing here.

    Validating here turns a server-side exception -- which arrives as XML with
    an HTTP 200 and would otherwise surface as a corrupt tile -- into an error
    naming the formats that would have worked.

    Every advertised format is downloadable. Rasters are tiled and mosaicked;
    anything else -- KML, KMZ, SVG, the HTML viewers -- is fetched in a single
    request and written out verbatim.
    """
    if requested is None:
        return default

    wanted = (normalizer or normalize_image_format)(requested)
    if not advertised:
        return wanted

    for option in advertised:
        if option.strip().lower() == wanted.lower():
            return option

    raise ValidationError(
        f"Format {requested!r} is not offered here. Available: {', '.join(advertised)}"
    )


@dataclass(frozen=True)
class TileMatrixSetDef:
    """A tile matrix set and the identifiers of its zoom levels.

    The identifiers cannot be assumed to be the zoom number. Esri publishes
    ``0``, ``1``, ``2`` ...; GeoServer publishes ``EPSG:900913:0``,
    ``EPSG:900913:1`` ... and rejects a bare number with
    ``InvalidParameterValue: Unknown TILEMATRIX``.
    """

    identifier: str
    matrix_ids: tuple[str, ...]

    def matrix_for_zoom(self, zoom: int) -> str | None:
        """Return the TileMatrix identifier for a zoom level.

        Matches on a trailing level number first, since that is what every
        convention encodes, and falls back to positional order.
        """
        for matrix_id in self.matrix_ids:
            tail = matrix_id.rsplit(":", 1)[-1]
            if tail.isdigit() and int(tail) == zoom:
                return matrix_id
        if 0 <= zoom < len(self.matrix_ids):
            return self.matrix_ids[zoom]
        return None


@dataclass(frozen=True)
class WmtsLayer:
    """One layer advertised by a WMTS service."""

    identifier: str
    title: str
    formats: tuple[str, ...]
    tile_matrix_sets: tuple[str, ...]
    template: str | None
    styles: tuple[str, ...] = ()
    # Services commonly publish one RESTful template per image format; picking
    # the first would hand back PNG when the caller asked for JPEG.
    templates: tuple[tuple[str, str], ...] = ()

    def template_for(self, image_format: str | None) -> str | None:
        """Return the RESTful template matching a format, if one is published."""
        wanted = image_format or self.default_format
        for advertised, template in self.templates:
            if advertised == wanted:
                return template
        return self.template

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
    matrix_sets: dict[str, TileMatrixSetDef] = field(default_factory=dict)

    def tile_matrix_id(self, matrix_set: str, zoom: int) -> str:
        """Resolve the TileMatrix identifier for a zoom level in a matrix set.

        Falls back to the bare zoom number when the set was not advertised --
        which happens when the caller overrides the matrix set by hand.
        """
        definition = self.matrix_sets.get(matrix_set)
        if definition is None:
            return str(zoom)
        return definition.matrix_for_zoom(zoom) or str(zoom)

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

        names = sorted(self.layers)
        available = ", ".join(names[:12]) + (" ..." if len(names) > 12 else "")
        raise ValidationError(
            f"Layer {identifier!r} is not published by this service."
            f"{suggest_layer(identifier, names)} Available: {available}"
        )


def _local(tag: str) -> str:
    """Return an element tag without its namespace."""
    return tag.rsplit("}", 1)[-1]


def suggest_layer(wanted: str, available: Sequence[str]) -> str:
    """Suggest a near match for a layer name, chiefly across workspace prefixes.

    A workspace-scoped GeoServer endpoint publishes ``DRYGEO2`` while the
    global one and the tile cache publish the same layer as ``mta:DRYGEO2``, so
    a name copied from one fails against the other. The bare names match, which
    is enough to point at the right one.
    """
    bare = wanted.rsplit(":", 1)[-1].lower()
    for name in available:
        if name.rsplit(":", 1)[-1].lower() == bare and name.lower() != wanted.lower():
            return f" Did you mean {name!r}?"
    return ""


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

        templates: list[tuple[str, str]] = []
        for resource in _children(node, "ResourceURL"):
            if resource.get("resourceType") != "tile":
                continue
            template = resource.get("template")
            if template:
                templates.append((resource.get("format") or "", template))

        capabilities.layers[identifier] = WmtsLayer(
            identifier=identifier,
            title=_text(_child(node, "Title")) or identifier,
            formats=formats,
            tile_matrix_sets=matrix_sets,
            template=templates[0][1] if templates else None,
            styles=styles,
            templates=tuple(templates),
        )

    for node in _children(_descendant(root, "Contents"), "TileMatrixSet"):
        set_id = _text(_child(node, "Identifier"))
        matrix_ids = tuple(
            _text(_child(matrix, "Identifier"))
            for matrix in _children(node, "TileMatrix")
            if _text(_child(matrix, "Identifier"))
        )
        if set_id and matrix_ids:
            capabilities.matrix_sets[set_id] = TileMatrixSetDef(set_id, matrix_ids)

    if not capabilities.layers:
        raise EndpointDiscoveryError(f"The WMTS capabilities at {service_url} advertise no layers.")
    return capabilities


@dataclass(frozen=True)
class WmsLayer:
    """One requestable layer advertised by a WMS service."""

    name: str
    title: str
    crs: tuple[str, ...]
    bounds: BoundingBox | None
    queryable: bool = False

    def supports_wgs84(self) -> bool:
        """Whether the layer advertises a CRS this downloader can request."""
        wanted = {"epsg:4326", "crs:84", "epsg:3857", "epsg:900913"}
        return any(code.strip().lower() in wanted for code in self.crs)


@dataclass
class WmsCapabilities:
    """The parts of a WMS capabilities document this downloader needs."""

    service_url: str
    title: str
    version: str
    layers: list[WmsLayer] = field(default_factory=list)
    # WMS advertises GetMap formats once for the whole service, unlike WMTS
    # which advertises them per layer.
    formats: tuple[str, ...] = ()

    @property
    def default_format(self) -> str:
        """Pick the most useful advertised GetMap format."""
        for preferred in ("image/png", "image/jpeg", "image/webp"):
            if preferred in self.formats:
                return preferred
        return self.formats[0] if self.formats else "image/png"

    def layer(self, name: str) -> WmsLayer:
        """Return a layer by name, case-insensitively."""
        for item in self.layers:
            if item.name == name:
                return item
        lowered = name.lower()
        for item in self.layers:
            if item.name.lower() == lowered:
                return item
        names = [item.name for item in self.layers]
        available = ", ".join(names[:12]) + (" ..." if len(names) > 12 else "")
        raise ValidationError(
            f"Layer {name!r} is not published by this service."
            f"{suggest_layer(name, names)} Available: {available}"
        )


def _geographic_bounds(node: ET.Element) -> BoundingBox | None:
    """Extract a layer's geographic extent from either WMS spelling.

    1.3.0 uses ``EX_GeographicBoundingBox`` with child elements; 1.1.1 uses
    ``LatLonBoundingBox`` with attributes.
    """
    exgeo = _child(node, "EX_GeographicBoundingBox")
    if exgeo is not None:
        try:
            return BoundingBox(
                west=float(_text(_child(exgeo, "westBoundLongitude"))),
                east=float(_text(_child(exgeo, "eastBoundLongitude"))),
                south=float(_text(_child(exgeo, "southBoundLatitude"))),
                north=float(_text(_child(exgeo, "northBoundLatitude"))),
            )
        except (TypeError, ValueError):
            return None

    latlon = _child(node, "LatLonBoundingBox")
    if latlon is not None:
        try:
            return BoundingBox(
                west=float(latlon.get("minx", "")),
                south=float(latlon.get("miny", "")),
                east=float(latlon.get("maxx", "")),
                north=float(latlon.get("maxy", "")),
            )
        except (TypeError, ValueError):
            return None
    return None


def parse_wms_capabilities(xml: str, service_url: str) -> WmsCapabilities:
    """Parse a WMS ``GetCapabilities`` document.

    WMS nests layers, and only those carrying a ``<Name>`` can be requested --
    the rest are grouping nodes. Children inherit their parents' CRS list and
    fall back to a parent's extent, so the tree is walked rather than flattened.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise EndpointDiscoveryError(
            f"Could not parse WMS capabilities from {service_url}: {exc}"
        ) from exc

    service = _descendant(root, "Service")
    title = _text(_child(service, "Title")) or "WMS service"
    version = root.get("version") or "1.3.0"

    get_map = None
    request = _descendant(root, "Request")
    if request is not None:
        get_map = _child(request, "GetMap")
    formats = tuple(_text(node) for node in _children(get_map, "Format") if _text(node))

    capabilities = WmsCapabilities(
        service_url=service_url, title=title, version=version, formats=formats
    )

    def walk(
        node: ET.Element, inherited_crs: tuple[str, ...], inherited_bounds: BoundingBox | None
    ) -> None:
        """Collect named layers, propagating inherited CRS and extent."""
        # 1.3.0 spells it CRS, 1.1.1 spells it SRS.
        own = tuple(
            _text(element)
            for element in node
            if _local(element.tag) in {"CRS", "SRS"} and _text(element)
        )
        crs = tuple(dict.fromkeys(inherited_crs + own))
        bounds = _geographic_bounds(node) or inherited_bounds

        name = _text(_child(node, "Name"))
        if name:
            capabilities.layers.append(
                WmsLayer(
                    name=name,
                    title=_text(_child(node, "Title")) or name,
                    crs=crs,
                    bounds=bounds,
                    queryable=node.get("queryable") in {"1", "true"},
                )
            )

        for child in _children(node, "Layer"):
            walk(child, crs, bounds)

    capability = _descendant(root, "Capability")
    for layer_node in _children(capability, "Layer"):
        walk(layer_node, (), None)

    if not capabilities.layers:
        raise EndpointDiscoveryError(
            f"The WMS capabilities at {service_url} advertise no named layers."
        )
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
    # Never the bare zoom: GeoServer names its levels `EPSG:900913:16` and
    # answers a bare `16` with `InvalidParameterValue: Unknown TILEMATRIX`.
    matrix_id = capabilities.tile_matrix_id(matrix_set, tile.z)

    template = layer.template_for(image_format)
    if template:
        return (
            template.replace("{TileMatrixSet}", matrix_set)
            .replace("{TileMatrix}", matrix_id)
            .replace("{TileRow}", str(tile.y))
            .replace("{TileCol}", str(tile.x))
            .replace("{Style}", chosen_style)
            .replace("{style}", chosen_style)
            .replace("{Layer}", layer.identifier)
            .replace("{layer}", layer.identifier)
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
            "TILEMATRIX": matrix_id,
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


def describe_service_error(response: "httpx.Response") -> str:
    """Extract the explanation an OGC server puts in a failed response body.

    Servers are consistently helpful here and the information is consistently
    thrown away: a bare status code says nothing, while the body carries
    ``Unknown TILEMATRIX 16`` or ``tile dimensions 1024x1024 do not match those
    of the grid set (256x256)``. Three encodings are seen in the wild -- OGC's
    XML exception report, GeoWebCache's HTML error page, and plain text.
    """
    body = response.text.strip()
    if not body:
        return f"HTTP {response.status_code} with an empty body"

    if body.startswith("<?xml") or "<ServiceException" in body or "<ExceptionReport" in body:
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            pass
        else:
            # The attributes live on the containing element, while the message
            # may be either its own text (WMS `ServiceException`) or a child
            # (OWS `Exception` wrapping an `ExceptionText`).
            for name in ("Exception", "ServiceException"):
                node = _descendant(root, name)
                if node is None:
                    continue
                text = (
                    _text(_child(node, "ExceptionText"))
                    or (node.text or "").strip()
                    or node.get("exceptionCode")
                    or node.get("code")
                    or ""
                )
                locator = node.get("locator") or ""
                if text:
                    return f"{text} ({locator})" if locator else text

    # GeoWebCache renders its errors as an HTML page with the reason in an <h4>.
    heading = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", body, re.S | re.I)
    if heading:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", heading.group(1))).strip()

    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
    return plain[:300] or f"HTTP {response.status_code}"


def sibling_service_url(url: str, service: str) -> str:
    """Point a service URL at a different OGC service on the same server.

    GeoServer publishes ``/geoserver/wms``, ``/geoserver/wfs`` and the combined
    ``/geoserver/ows`` side by side, so the companion endpoint is usually the
    same path with its last segment swapped. ``ows`` serves everything and is
    left alone; anything unrecognised is returned unchanged rather than
    mangled, and the caller can always pass the URL explicitly.
    """
    parts = urlparse(url)
    segments = parts.path.rstrip("/").split("/")
    if not segments:
        return url

    last = segments[-1].lower()
    if last == "ows":
        return url
    if last in {"wms", "wfs", "wmts", "wcs"}:
        segments[-1] = service
        return urlunparse(parts._replace(path="/".join(segments)))
    return url


def endpoint_hint(url: str) -> str:
    """Return advice for endpoints that are commonly mistaken for a plain WMS.

    ``/gwc/service/wms`` is GeoWebCache's WMS-C interface, not a general WMS.
    It serves only requests matching its cached grid exactly -- 256x256 pixels
    on an aligned bounding box -- and speaks WMS 1.1.1 only. People reach it by
    copying the WMS link from a GeoServer tile-caching page.
    """
    if "/gwc/service/wms" in url.lower():
        return (
            "\nThat endpoint is GeoWebCache's tile-aligned WMS-C interface, which only "
            "serves 256x256 requests on grid-aligned bounding boxes. For arbitrary "
            "extents use the plain WMS endpoint (usually /geoserver/<workspace>/wms), "
            "or fetch the tiles with the `wmts` command against /gwc/service/wmts."
        )
    return ""


class OgcClient:
    """Fetches capabilities and imagery from arbitrary WMS/WMTS services."""

    def __init__(self, http: AsyncHttpClient) -> None:
        """Wire the client to the shared HTTP transport."""
        self._http = http

    async def _get(self, url: str, accept: str, description: str) -> "httpx.Response":
        """GET a URL, converting an HTTP error into the server's own reason."""
        try:
            return await self._http.get(url, accept=accept, description=description)
        except httpx.HTTPStatusError as exc:
            raise ServiceRequestError(
                f"{description} failed: HTTP {exc.response.status_code} -- "
                f"{describe_service_error(exc.response)}{endpoint_hint(url)}"
            ) from exc

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

        response = await self._get(url, "text/xml,application/xml,*/*", "WMTS capabilities")
        return parse_wmts_capabilities(response.text, service_url)

    async def wms_capabilities(self, service_url: str, version: str = "1.3.0") -> WmsCapabilities:
        """Fetch and parse a WMS capabilities document."""
        url = service_url
        if "request=" not in url.lower():
            url = _with_query(
                url, {"SERVICE": "WMS", "REQUEST": "GetCapabilities", "VERSION": version}
            )

        response = await self._get(url, "text/xml,application/xml,*/*", "WMS capabilities")
        return parse_wms_capabilities(response.text, service_url)

    async def fetch_image(self, url: str, description: str, expect_raster: bool = True) -> bytes:
        """Fetch an image, rejecting an XML service exception dressed as a tile.

        OGC servers report errors as XML with a 200 status, so a response that
        is not actually an image has to be caught here or it would be pasted
        into the mosaic as a corrupt tile.
        """
        response = await self._get(url, "image/*,*/*", description)
        content_type = response.headers.get("Content-Type", "").lower()

        if not response.content:
            raise EndpointDiscoveryError(f"{description} returned an empty body")

        # Not every XML body is an error. KML is XML, and so is SVG; treating
        # any XML as an exception reported a perfectly valid KML document as
        # "a service exception". Look for an actual exception document.
        head = response.content[:2048].decode("utf-8", errors="replace")
        if any(
            marker in head
            for marker in ("ServiceExceptionReport", "ExceptionReport", "<ServiceException")
        ):
            raise ServiceRequestError(
                f"{description} returned a service exception: {describe_service_error(response)}"
            )

        if expect_raster:
            # Reached only when a raster was requested. A non-image reply here
            # means the service ignored the requested format, and the bytes
            # would fail deep inside the decoder rather than at the boundary.
            base_type = content_type.split(";")[0].strip()
            if base_type and not is_raster_format(base_type):
                raise ValidationError(
                    f"{description} returned {base_type!r} when a raster image was "
                    "requested. The service may not honour that format."
                )
        return response.content
