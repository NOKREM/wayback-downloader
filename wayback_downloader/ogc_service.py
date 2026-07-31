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
    normalize_image_format,
    resolve_format,
    wms_getmap_url,
    wmts_tile_url,
)
from wayback_downloader.config import Settings, get_settings
from wayback_downloader.exceptions import ImageryUnavailableError, ValidationError
from wayback_downloader.gis import stitch
from wayback_downloader.gis.projection import ground_resolution
from wayback_downloader.gis.tiles import grid_bounds, plan_grid
from wayback_downloader.models import BoundingBox, Coordinate
from wayback_downloader.utils.cache import CacheStore
from wayback_downloader.utils.http import AsyncHttpClient
from wayback_downloader.utils.logger import get_logger
from wayback_downloader.utils.naming import safe_stem
from wayback_downloader.utils.progress import NullProgress, ProgressReporter

logger = get_logger(__name__)

_FORMAT_SUFFIX = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/tiff": "tif",
}


@dataclass
class OgcResult:
    """An image downloaded from an OGC service, plus its sidecar."""

    image: Image.Image
    image_path: Path
    metadata_path: Path
    bounds: BoundingBox
    request_count: int


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

        semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        tiles: dict[tuple[int, int], bytes] = {}
        failures = 0
        lock = asyncio.Lock()

        with self.progress.task(f"WMTS tiles z{zoom}", total=len(placements)) as task:

            async def fetch(offset: tuple[int, int], index: object) -> None:
                nonlocal failures
                url = wmts_tile_url(
                    capabilities, layer, chosen_set, index, chosen_format, style  # type: ignore[arg-type]
                )
                try:
                    async with semaphore:
                        payload = await self.client.fetch_image(url, f"tile {index}")
                except Exception as exc:
                    async with lock:
                        failures += 1
                    logger.debug("WMTS tile %s failed: %s", index, exc)
                else:
                    async with lock:
                        tiles[offset] = payload
                task.advance(1)

            await asyncio.gather(*(fetch((p.offset_x, p.offset_y), p.index) for p in placements))

        if not tiles:
            raise ImageryUnavailableError(
                f"Every tile request to {service_url} failed for layer {layer.identifier!r}."
            )
        if failures:
            logger.warning("%d of %d WMTS tiles failed", failures, len(placements))

        image = await asyncio.to_thread(stitch.build_image, grid, tiles)
        bounds = grid_bounds(grid)

        suffix = _FORMAT_SUFFIX.get(chosen_format, "png")
        name = stem or safe_stem(f"wmts_{layer.identifier}_{zoom}", "wmts")
        return await self._write(
            image,
            bounds,
            output_dir,
            name,
            suffix,
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
    ) -> OgcResult:
        """Download a bounding box from a WMS service.

        A request larger than a server will usually accept is split into a grid
        of ``GetMap`` calls and reassembled, so the caller can ask for an image
        far bigger than any single request allows.

        Unless ``validate`` is disabled, the layer names and the requested
        format are checked against the service's capabilities first. Both would
        otherwise fail as an XML service exception carrying HTTP 200.
        """
        if width <= 0 or height <= 0:
            raise ValidationError("WMS image dimensions must be positive.")

        chosen_format = normalize_image_format(image_format) if image_format else "image/png"
        if validate:
            capabilities = await self.wms_capabilities(service_url, version)
            for name in (part.strip() for part in layers.split(",") if part.strip()):
                capabilities.layer(name)
            chosen_format = resolve_format(
                image_format, capabilities.formats, capabilities.default_format
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

        suffix = _FORMAT_SUFFIX.get(chosen_format, "png")
        name = stem or safe_stem(f"wms_{layers}", "wms")
        return await self._write(
            image,
            bbox,
            output_dir,
            name,
            suffix,
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

    async def _write(
        self,
        image: Image.Image,
        bounds: BoundingBox,
        output_dir: Path | None,
        stem: str,
        suffix: str,
        request_count: int,
        extra: dict,
    ) -> OgcResult:
        """Write the image and its JSON sidecar."""
        target = output_dir or self.settings.output_dir
        target.mkdir(parents=True, exist_ok=True)

        image_format = "jpg" if suffix in {"jpg", "jpeg"} else "png"
        image_path = target / f"{stem}.{image_format}"
        await asyncio.to_thread(
            stitch.save_image,
            image,
            image_path,
            image_format,
            self.settings.jpeg_quality,
            self.settings.png_compress_level,
        )

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
