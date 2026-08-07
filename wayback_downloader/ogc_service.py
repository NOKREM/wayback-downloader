"""Downloading imagery from arbitrary WMS and WMTS services.

Kept separate from :mod:`wayback_downloader.service`, which is specific to the
Wayback archive's release model. What the two share is everything below the
protocol: the Web Mercator maths, the tile grid planner, the mosaicker and the
encoder are all reused unchanged.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from PIL import Image

from wayback_downloader import __version__
from wayback_downloader.api.ogc import (
    MAX_WMS_PIXELS,
    OgcClient,
    WmsCapabilities,
    WmtsCapabilities,
    format_extension,
    is_raster_format,
    normalize_image_format,
    pillow_writer,
    resolve_format,
    sibling_service_url,
    wms_getmap_url,
    wmts_tile_url,
)
from wayback_downloader.export.kml import MergeReport, merge_attributes
from wayback_downloader.api.ogc import _with_query
from wayback_downloader.api.wfs import (
    WfsCapabilities,
    normalize_output_format,
    output_extension,
    parse_wfs_capabilities,
    resolve_output_format,
    styling_note,
    summarize_features,
    wfs_getfeature_url,
)
from wayback_downloader.config import Settings, get_settings
from wayback_downloader.exceptions import ImageryUnavailableError, ValidationError
from wayback_downloader.export import geotiff
from wayback_downloader.gis import stitch
from wayback_downloader.gis.projection import ground_resolution
from wayback_downloader.gis.tiles import grid_bounds, plan_grid
from wayback_downloader.models import BoundingBox, Coordinate, TileIndex
from wayback_downloader.utils.cache import CacheStore
from wayback_downloader.utils.http import AsyncHttpClient
from wayback_downloader.utils.logger import get_logger
from wayback_downloader.utils.naming import safe_stem
from wayback_downloader.utils.progress import NullProgress, ProgressReporter

logger = get_logger(__name__)


@dataclass
class OgcResult:
    """Something downloaded from an OGC service, plus its sidecar.

    ``image`` is ``None`` for non-raster formats, which are written through
    unchanged rather than decoded -- there is no bitmap to hand back.
    """

    image_path: Path
    metadata_path: Path
    bounds: BoundingBox
    request_count: int
    image: Image.Image | None = None

    @property
    def size(self) -> tuple[int, int] | None:
        """Pixel dimensions, or ``None`` when nothing was rasterised."""
        return self.image.size if self.image is not None else None


class OgcService:
    """Downloads imagery from any compliant WMS or WMTS endpoint."""

    def __init__(
        self,
        settings: Settings | None = None,
        use_cache: bool = True,
        progress: ProgressReporter | None = None,
    ) -> None:
        """Construct the service and its HTTP transport."""
        self.settings = settings or get_settings()
        self.settings.ensure_directories()
        self.progress = progress or NullProgress()
        self._cache = CacheStore(
            self.settings.cache_dir, size_limit=self.settings.cache_size_limit, enabled=use_cache
        )
        self._http = AsyncHttpClient(self.settings)
        self.client = OgcClient(self._http)

    async def __aenter__(self) -> "OgcService":
        """Enter the async context."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Release the HTTP pool and close the cache."""
        await self.close()

    async def close(self) -> None:
        """Release every resource held by the service."""
        await self._http.aclose()
        self._cache.close()

    async def capabilities(self, service_url: str) -> WmtsCapabilities:
        """Fetch and parse a WMTS service's capabilities."""
        return await self.client.wmts_capabilities(service_url)

    async def wms_capabilities(self, service_url: str, version: str = "1.3.0") -> WmsCapabilities:
        """Fetch and parse a WMS service's capabilities."""
        return await self.client.wms_capabilities(service_url, version)

    async def wfs_capabilities(self, service_url: str, version: str = "2.0.0") -> WfsCapabilities:
        """Fetch and parse a WFS service's capabilities."""
        url = service_url
        if "request=" not in url.lower():
            url = _with_query(
                url, {"SERVICE": "WFS", "REQUEST": "GetCapabilities", "VERSION": version}
            )
        response = await self.client._get(url, "text/xml,application/xml,*/*", "WFS capabilities")
        return parse_wfs_capabilities(response.text, service_url)

    async def download_wfs(
        self,
        service_url: str,
        type_name: str,
        version: str = "2.0.0",
        bbox: BoundingBox | None = None,
        output_format: str | None = None,
        max_features: int | None = None,
        start_index: int | None = None,
        cql_filter: str | None = None,
        sort_by: str | None = None,
        property_names: str | None = None,
        output_dir: Path | None = None,
        stem: str | None = None,
        validate: bool = True,
    ) -> OgcResult:
        """Download features from a WFS service.

        Nothing here is rendered: the response is vector data and is written
        through byte for byte. Only GeoJSON is inspected afterwards, to report
        how many features arrived.
        """
        chosen_format = normalize_output_format(output_format) if output_format else None
        extent = bbox

        if validate:
            capabilities = await self.wfs_capabilities(service_url, version)
            feature_type = capabilities.feature_type(type_name)
            type_name = feature_type.name
            chosen_format = resolve_output_format(
                output_format, capabilities.formats, capabilities.default_format
            )
            if extent is None:
                extent = feature_type.bounds

        url = wfs_getfeature_url(
            service_url,
            type_name,
            version=version,
            bbox=bbox,
            output_format=chosen_format,
            max_features=max_features,
            start_index=start_index,
            cql_filter=cql_filter,
            sort_by=sort_by,
            property_names=property_names,
        )

        with self.progress.task("WFS features", total=1) as task:
            payload = await self.client.fetch_image(url, "GetFeature", expect_raster=False)
            task.advance(1)

        resolved_format = chosen_format or "application/json"
        if note := styling_note(resolved_format):
            logger.warning("%s", note)
        summary = summarize_features(payload, resolved_format)
        name = stem or safe_stem(f"wfs_{type_name}", "wfs")
        target = output_dir or self.settings.output_dir
        target.mkdir(parents=True, exist_ok=True)

        document_path = target / f"{name}.{output_extension(resolved_format)}"
        document_path.write_bytes(payload)
        logger.info(
            "Wrote %s (%s)",
            document_path.name,
            (
                f"{summary['feature_count']} feature(s)"
                if "feature_count" in summary
                else f"{len(payload)} bytes"
            ),
        )

        return self._write_sidecar(
            document_path,
            extent or BoundingBox(west=-180, south=-85, east=180, north=85),
            1,
            {
                "service": "WFS",
                "service_url": service_url,
                "type_name": type_name,
                "version": version,
                "format": resolved_format,
                "rasterised": False,
                "requested_bbox": (
                    None
                    if bbox is None
                    else {
                        "west": bbox.west,
                        "south": bbox.south,
                        "east": bbox.east,
                        "north": bbox.north,
                    }
                ),
                "max_features": max_features,
                "cql_filter": cql_filter,
                **summary,
            },
        )

    async def download_styled_kml(
        self,
        service_url: str,
        layer: str,
        bbox: BoundingBox,
        width: int = 2048,
        height: int = 2048,
        wfs_url: str | None = None,
        wms_version: str = "1.3.0",
        wfs_version: str = "2.0.0",
        max_features: int | None = None,
        cql_filter: str | None = None,
        output_dir: Path | None = None,
        stem: str | None = None,
    ) -> tuple[OgcResult, MergeReport]:
        """Produce a KML carrying both the layer's styling and its attributes.

        Fetches the styled KML from WMS and the features from WFS, then splices
        the attributes into the styled document by feature id. The two requests
        run concurrently since neither depends on the other.

        ``width`` and ``height`` do not size an image here -- nothing is
        rasterised -- but WMS requires them and uses them to decide which
        features fall inside the view, so they are kept generous.
        """
        features_url = wfs_url or sibling_service_url(service_url, "wfs")

        styled_request = self.client.fetch_image(
            wms_getmap_url(
                service_url,
                layer,
                bbox,
                width,
                height,
                version=wms_version,
                image_format="application/vnd.google-earth.kml+xml",
            ),
            "GetMap (styled KML)",
            expect_raster=False,
        )
        features_request = self.client.fetch_image(
            wfs_getfeature_url(
                features_url,
                layer,
                version=wfs_version,
                bbox=bbox,
                output_format="application/json",
                max_features=max_features,
                cql_filter=cql_filter,
            ),
            "GetFeature (attributes)",
            expect_raster=False,
        )

        with self.progress.task("Styled KML", total=2) as task:
            styled, features = await asyncio.gather(styled_request, features_request)
            task.advance(2)

        merged, report = await asyncio.to_thread(merge_attributes, styled, features)
        logger.info("Merged styled KML: %s", report.summary())

        name = stem or safe_stem(f"kml_{layer}", "kml")
        target = output_dir or self.settings.output_dir
        target.mkdir(parents=True, exist_ok=True)

        document_path = target / f"{name}.kml"
        document_path.write_bytes(merged)

        result = self._write_sidecar(
            document_path,
            bbox,
            2,
            {
                "service": "WMS+WFS",
                "service_url": service_url,
                "wfs_url": features_url,
                "layer": layer,
                "format": "application/vnd.google-earth.kml+xml",
                "rasterised": False,
                "styled": True,
                "placemarks": report.placemarks,
                "features_matched": report.matched,
                "unmatched_placemarks": report.unmatched_placemarks,
                "attributes_attached": report.attributes_added,
                "byte_size": len(merged),
            },
        )
        return result, report

    async def download_wmts(
        self,
        service_url: str,
        coordinate: Coordinate,
        zoom: int,
        width: int,
        height: int,
        layer_id: str | None = None,
        matrix_set: str | None = None,
        image_format: str | None = None,
        style: str | None = None,
        output_dir: Path | None = None,
        stem: str | None = None,
    ) -> OgcResult:
        """Download a window centred on a coordinate from a WMTS layer.

        Requires the layer to publish a Web Mercator tile matrix set, since the
        tile addressing here is the standard XYZ pyramid. A layer in some other
        projection is reported rather than silently misplaced.
        """
        capabilities = await self.capabilities(service_url)
        layer = capabilities.layer(layer_id)
        chosen_format = resolve_format(image_format, layer.formats, layer.default_format)

        chosen_set = matrix_set or layer.web_mercator_matrix_set()
        if chosen_set is None:
            raise ImageryUnavailableError(
                f"Layer {layer.identifier!r} publishes no Web Mercator tile matrix set "
                f"(it offers: {', '.join(layer.tile_matrix_sets) or 'none'}). "
                "Pass --matrix-set explicitly if you know it is Web Mercator."
            )

        grid = plan_grid(coordinate, zoom, width, height, self.settings.tile_size)
        placements = grid.placements()
        logger.info(
            "WMTS %s: layer %s, matrix set %s, %d tiles at zoom %d",
            capabilities.title,
            layer.identifier,
            chosen_set,
            len(placements),
            zoom,
        )

        raster = is_raster_format(chosen_format)
        semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        tiles: dict[tuple[int, int], bytes] = {}
        addresses: dict[tuple[int, int], TileIndex] = {}
        failures = 0
        lock = asyncio.Lock()

        with self.progress.task(f"WMTS tiles z{zoom}", total=len(placements)) as task:

            async def fetch(offset: tuple[int, int], index: TileIndex) -> None:
                nonlocal failures
                url = wmts_tile_url(capabilities, layer, chosen_set, index, chosen_format, style)
                try:
                    async with semaphore:
                        payload = await self.client.fetch_image(
                            url, f"tile {index}", expect_raster=raster
                        )
                except Exception as exc:
                    async with lock:
                        failures += 1
                    logger.debug("WMTS tile %s failed: %s", index, exc)
                else:
                    async with lock:
                        tiles[offset] = payload
                        addresses[offset] = index
                task.advance(1)

            await asyncio.gather(*(fetch((p.offset_x, p.offset_y), p.index) for p in placements))

        if not tiles:
            raise ImageryUnavailableError(
                f"Every tile request to {service_url} failed for layer {layer.identifier!r}."
            )
        if failures:
            logger.warning("%d of %d WMTS tiles failed", failures, len(placements))

        bounds = grid_bounds(grid)
        name = stem or safe_stem(f"wmts_{layer.identifier}_{zoom}", "wmts")

        if not raster:
            return self._write_tiles(
                tiles,
                addresses,
                bounds,
                output_dir,
                name,
                chosen_format,
                {
                    "service": "WMTS",
                    "service_url": service_url,
                    "service_title": capabilities.title,
                    "layer": layer.identifier,
                    "tile_matrix_set": chosen_set,
                    "format": chosen_format,
                    "zoom": zoom,
                    "rasterised": False,
                },
            )

        image = await asyncio.to_thread(stitch.build_image, grid, tiles)
        return await self._write(
            image,
            bounds,
            output_dir,
            name,
            chosen_format,
            len(placements),
            {
                "service": "WMTS",
                "service_url": service_url,
                "service_title": capabilities.title,
                "layer": layer.identifier,
                "layer_title": layer.title,
                "tile_matrix_set": chosen_set,
                "format": chosen_format,
                "zoom": zoom,
                "latitude": coordinate.latitude,
                "longitude": coordinate.longitude,
                "ground_resolution_m_per_px": round(
                    ground_resolution(coordinate.latitude, zoom, self.settings.tile_size), 6
                ),
                "tile_count": len(placements),
                "failed_tiles": failures,
            },
        )

    async def download_wms(
        self,
        service_url: str,
        layers: str,
        bbox: BoundingBox,
        width: int,
        height: int,
        version: str = "1.3.0",
        image_format: str | None = None,
        styles: str = "",
        transparent: bool = False,
        output_dir: Path | None = None,
        stem: str | None = None,
        validate: bool = True,
        capabilities: WmsCapabilities | None = None,
    ) -> OgcResult:
        """Download a bounding box from a WMS service, in any offered format.

        A raster request larger than a server will usually accept is split into
        a grid of ``GetMap`` calls and reassembled, so the caller can ask for an
        image far bigger than any single request allows.

        A non-raster format -- KML, KMZ, SVG, an HTML viewer -- is a single
        document describing the whole extent. There is nothing to tile or
        stitch, so one request is made and the bytes are written through
        untouched.

        Unless ``validate`` is disabled, the layer names and the requested
        format are checked against the service's capabilities first. Both would
        otherwise fail as an XML service exception carrying HTTP 200.
        """
        if width <= 0 or height <= 0:
            raise ValidationError("WMS image dimensions must be positive.")

        chosen_format = normalize_image_format(image_format) if image_format else "image/png"
        if validate:
            # Reuse the caller's capabilities when it already has them: this
            # service load-balances across nodes whose layer lists differ, so a
            # second fetch can disagree with the one the caller validated
            # against and reject a layer it just accepted.
            if capabilities is None:
                capabilities = await self.wms_capabilities(service_url, version)
            for name in (part.strip() for part in layers.split(",") if part.strip()):
                capabilities.layer(name)
            chosen_format = resolve_format(
                image_format, capabilities.formats, capabilities.default_format
            )

        if not is_raster_format(chosen_format):
            return await self._download_wms_document(
                service_url,
                layers,
                bbox,
                width,
                height,
                version,
                chosen_format,
                styles,
                transparent,
                output_dir,
                stem,
            )

        columns = -(-width // MAX_WMS_PIXELS)
        rows = -(-height // MAX_WMS_PIXELS)
        chunk_w = -(-width // columns)
        chunk_h = -(-height // rows)

        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        pieces: dict[tuple[int, int], bytes] = {}
        lock = asyncio.Lock()
        total = columns * rows

        if total > 1:
            logger.info(
                "WMS request of %dx%d split into %dx%d GetMap calls", width, height, columns, rows
            )

        # Each chunk covers the matching fraction of the bounding box. Longitude
        # and latitude both interpolate linearly here because WMS takes the box
        # in geographic degrees, not projected metres.
        lon_span = bbox.east - bbox.west
        lat_span = bbox.north - bbox.south

        with self.progress.task("WMS requests", total=total) as task:

            async def fetch(column: int, row: int) -> None:
                left_px, top_px = column * chunk_w, row * chunk_h
                piece_w = min(chunk_w, width - left_px)
                piece_h = min(chunk_h, height - top_px)

                piece_box = BoundingBox(
                    west=bbox.west + lon_span * (left_px / width),
                    east=bbox.west + lon_span * ((left_px + piece_w) / width),
                    north=bbox.north - lat_span * (top_px / height),
                    south=bbox.north - lat_span * ((top_px + piece_h) / height),
                )
                url = wms_getmap_url(
                    service_url,
                    layers,
                    piece_box,
                    piece_w,
                    piece_h,
                    version=version,
                    image_format=chosen_format,
                    styles=styles,
                    transparent=transparent,
                )
                async with semaphore:
                    payload = await self.client.fetch_image(url, f"GetMap {column},{row}")
                async with lock:
                    pieces[(left_px, top_px)] = payload
                task.advance(1)

            await asyncio.gather(*(fetch(c, r) for r in range(rows) for c in range(columns)))

        def compose() -> Image.Image:
            """Paste every GetMap response into the output canvas."""
            for (left_px, top_px), payload in pieces.items():
                with Image.open(io.BytesIO(payload)) as piece:
                    canvas.paste(piece.convert("RGB"), (left_px, top_px))
            return canvas

        image = await asyncio.to_thread(compose)

        name = stem or safe_stem(f"wms_{layers}", "wms")
        return await self._write(
            image,
            bbox,
            output_dir,
            name,
            chosen_format,
            total,
            {
                "service": "WMS",
                "service_url": service_url,
                "layers": layers,
                "version": version,
                "format": chosen_format,
                "request_count": total,
            },
        )

    async def _download_wms_document(
        self,
        service_url: str,
        layers: str,
        bbox: BoundingBox,
        width: int,
        height: int,
        version: str,
        image_format: str,
        styles: str,
        transparent: bool,
        output_dir: Path | None,
        stem: str | None,
    ) -> OgcResult:
        """Fetch a non-raster GetMap response and write it out verbatim.

        KML, SVG and the rest describe the whole extent in one document, so
        splitting the request would produce fragments that cannot be recombined.
        """
        url = wms_getmap_url(
            service_url,
            layers,
            bbox,
            width,
            height,
            version=version,
            image_format=image_format,
            styles=styles,
            transparent=transparent,
        )
        with self.progress.task("WMS document", total=1) as task:
            payload = await self.client.fetch_image(url, "GetMap", expect_raster=False)
            task.advance(1)

        extension = format_extension(image_format)
        name = stem or safe_stem(f"wms_{layers}", "wms")
        target = output_dir or self.settings.output_dir
        target.mkdir(parents=True, exist_ok=True)

        document_path = target / f"{name}.{extension}"
        document_path.write_bytes(payload)
        logger.info("Wrote %s (%d bytes, not rasterised)", document_path.name, len(payload))

        return self._write_sidecar(
            document_path,
            bbox,
            1,
            {
                "service": "WMS",
                "service_url": service_url,
                "layers": layers,
                "version": version,
                "format": image_format,
                "rasterised": False,
                "request_count": 1,
                "byte_size": len(payload),
            },
        )

    def _write_tiles(
        self,
        tiles: dict[tuple[int, int], bytes],
        addresses: dict[tuple[int, int], TileIndex],
        bounds: BoundingBox,
        output_dir: Path | None,
        stem: str,
        image_format: str,
        extra: dict,
    ) -> OgcResult:
        """Write non-raster WMTS tiles individually into a directory.

        A KML or SVG tile cannot be pasted into a mosaic, and concatenating the
        documents would produce nothing valid, so each tile is kept as its own
        file named by its address.
        """
        target = (output_dir or self.settings.output_dir) / stem
        target.mkdir(parents=True, exist_ok=True)
        extension = format_extension(image_format)

        for offset, payload in sorted(tiles.items()):
            index = addresses[offset]
            (target / f"{index.z}_{index.x}_{index.y}.{extension}").write_bytes(payload)

        logger.info("Wrote %d tile(s) to %s (not rasterised)", len(tiles), target)
        return self._write_sidecar(
            target / f"{stem}.{extension}",
            bounds,
            len(tiles),
            {**extra, "tile_count": len(tiles), "tile_directory": str(target)},
            write_file=False,
        )

    def _write_sidecar(
        self,
        image_path: Path,
        bounds: BoundingBox,
        request_count: int,
        extra: dict,
        write_file: bool = True,
    ) -> OgcResult:
        """Write the JSON sidecar describing a download.

        ``write_file`` is false when the payload is a directory of tiles rather
        than a single file; ``image_path`` then names only where the sidecar
        goes and what the download was called.
        """
        metadata = {
            **extra,
            "bounds_wgs84": {
                "west": round(bounds.west, 8),
                "south": round(bounds.south, 8),
                "east": round(bounds.east, 8),
                "north": round(bounds.north, 8),
            },
            "image_file": image_path.name,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "tool_version": __version__,
        }
        metadata_path = image_path.with_suffix(".json")
        metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return OgcResult(
            image_path=image_path,
            metadata_path=metadata_path,
            bounds=bounds,
            request_count=request_count,
        )

    def _save_raster(self, image: Image.Image, path: Path, mime: str) -> None:
        """Encode a mosaic in the requested raster format. Runs on a thread."""
        writer = pillow_writer(mime)
        if writer in {"PNG", "JPEG"} or writer is None:
            # The tuned PNG and JPEG paths live in the stitcher; an unwritable
            # format falls back to PNG rather than failing the download.
            stitch.save_image(
                image,
                path,
                "jpg" if writer == "JPEG" else "png",
                self.settings.jpeg_quality,
                self.settings.png_compress_level,
            )
            if writer is None:
                logger.warning("Cannot encode %s; wrote PNG instead", mime)
            return

        # GIF and BMP have no alpha, and JPEG-like flattening applies to them.
        payload = image if writer in {"WEBP", "TIFF"} else image.convert("RGB")
        payload.save(path, format=writer)

    async def _write(
        self,
        image: Image.Image,
        bounds: BoundingBox,
        output_dir: Path | None,
        stem: str,
        mime: str,
        request_count: int,
        extra: dict,
    ) -> OgcResult:
        """Write the mosaic in the requested format, plus its JSON sidecar.

        The output keeps the format that was asked for wherever Pillow can
        write it, rather than collapsing everything to PNG. A GeoTIFF request
        goes through the georeferencing writer so the result carries its extent.
        """
        target = output_dir or self.settings.output_dir
        target.mkdir(parents=True, exist_ok=True)
        image_path = target / f"{stem}.{format_extension(mime)}"

        if mime.split(";")[0].strip().lower() == "image/geotiff":
            await asyncio.to_thread(geotiff.write_geotiff, image, bounds, image_path)
        else:
            await asyncio.to_thread(self._save_raster, image, image_path, mime)

        metadata = {
            **extra,
            "bounds_wgs84": {
                "west": round(bounds.west, 8),
                "south": round(bounds.south, 8),
                "east": round(bounds.east, 8),
                "north": round(bounds.north, 8),
            },
            "image_size": {"width": image.width, "height": image.height},
            "image_file": image_path.name,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "tool_version": __version__,
        }
        metadata_path = target / f"{stem}.json"
        metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        logger.info("Wrote %s", image_path.name)
        return OgcResult(
            image=image,
            image_path=image_path,
            metadata_path=metadata_path,
            bounds=bounds,
            request_count=request_count,
        )
