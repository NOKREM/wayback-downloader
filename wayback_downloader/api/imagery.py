"""Rendering a Wayback release into a finished image.

This is the seam between the network layer and the pixel layer: it plans the
tile grid for a request, hands it to the downloader, and stitches the result
into an image cropped exactly around the requested coordinate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from PIL import Image

from wayback_downloader.api.downloader import DownloadStats, ProgressHook, TileDownloader
from wayback_downloader.config import Settings
from wayback_downloader.gis import stitch
from wayback_downloader.gis.projection import ground_resolution
from wayback_downloader.gis.tiles import grid_bounds, plan_grid, plan_grid_for_bbox
from wayback_downloader.models import (
    BoundingBox,
    Coordinate,
    TileGrid,
    WaybackRelease,
)
from wayback_downloader.utils.cache import CacheStore
from wayback_downloader.utils.http import AsyncHttpClient
from wayback_downloader.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class RenderedImage:
    """A finished image together with how it was produced."""

    image: Image.Image
    release: WaybackRelease
    grid: TileGrid
    bounds: BoundingBox
    stats: DownloadStats
    ground_resolution_m: float

    @property
    def size(self) -> tuple[int, int]:
        """Pixel dimensions of the rendered image."""
        return self.image.size


class ImageryService:
    """Turns a release plus a viewport into a stitched, cropped image."""

    def __init__(self, http: AsyncHttpClient, settings: Settings, cache: CacheStore) -> None:
        """Wire the imagery service to its downloader and settings."""
        self._settings = settings
        self._downloader = TileDownloader(http, settings, cache)

    async def render(
        self,
        release: WaybackRelease,
        coordinate: Coordinate,
        zoom: int,
        width: int,
        height: int,
        on_progress: ProgressHook | None = None,
    ) -> RenderedImage:
        """Render a pixel window centred on a coordinate from one release."""
        grid = plan_grid(coordinate, zoom, width, height, self._settings.tile_size)
        return await self._render_grid(release, grid, coordinate.latitude, on_progress)

    async def render_bbox(
        self,
        release: WaybackRelease,
        bbox: BoundingBox,
        zoom: int,
        on_progress: ProgressHook | None = None,
    ) -> RenderedImage:
        """Render the imagery covering a bounding box from one release."""
        grid = plan_grid_for_bbox(bbox, zoom, self._settings.tile_size)
        return await self._render_grid(release, grid, bbox.center.latitude, on_progress)

    async def _render_grid(
        self,
        release: WaybackRelease,
        grid: TileGrid,
        latitude: float,
        on_progress: ProgressHook | None,
    ) -> RenderedImage:
        """Download and compose a planned grid."""
        logger.debug(
            "Rendering release %s (%s): %d tiles at zoom %d",
            release.release_num,
            release.release_date,
            grid.tile_count,
            grid.z,
        )
        tiles, stats = await self._downloader.download_grid(release, grid, on_progress)
        # Decoding and pasting hundreds of tiles is CPU work that would
        # otherwise stall every other coroutine on the loop.
        image = await asyncio.to_thread(stitch.build_image, grid, tiles)

        return RenderedImage(
            image=image,
            release=release,
            grid=grid,
            bounds=grid_bounds(grid),
            stats=stats,
            ground_resolution_m=ground_resolution(latitude, grid.z, self._settings.tile_size),
        )

    def plan(self, coordinate: Coordinate, zoom: int, width: int, height: int) -> TileGrid:
        """Plan a grid without downloading anything, for dry runs and estimates."""
        return plan_grid(coordinate, zoom, width, height, self._settings.tile_size)
