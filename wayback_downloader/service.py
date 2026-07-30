"""End-to-end orchestration.

Everything the CLI does routes through :class:`WaybackService`. It owns the one
HTTP client and cache for a run, resolves a user request to a concrete release,
renders the image and writes the image plus its JSON sidecar.

Keeping the workflow here rather than in the CLI means the same operations are
usable as a library:

    async with WaybackService() as service:
        result = await service.download(request)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Callable, Iterable, Sequence

from PIL import Image

from wayback_downloader import __version__
from wayback_downloader.api.imagery import ImageryService, RenderedImage
from wayback_downloader.api.metadata import MetadataClient
from wayback_downloader.api.wayback import (
    WaybackCatalog,
    filter_by_date_range,
    match_nearest_release,
)
from wayback_downloader.config import Settings, get_settings
from wayback_downloader.exceptions import ImageryUnavailableError, WaybackError
from wayback_downloader.export import animation, geotiff
from wayback_downloader.gis import stitch
from wayback_downloader.gis.tiles import describe_resolution
from wayback_downloader.models import (
    BoundingBox,
    Coordinate,
    DownloadRequest,
    ImageryMetadata,
    OutputMetadata,
    WaybackRelease,
)
from wayback_downloader.utils.cache import CacheStore
from wayback_downloader.utils.http import AsyncHttpClient
from wayback_downloader.utils.logger import get_logger
from wayback_downloader.utils.progress import ProgressReporter, NullProgress

logger = get_logger(__name__)

# Zoom used to pick the release whose coverage bands define the "all" range.
# Mid-pyramid: high enough to sit inside detailed imagery footprints, low enough
# to have imagery essentially everywhere on land.
ZOOM_PROBE_LEVEL = 16

# Chooses which of a zoom level's candidate releases to actually download.
ReleaseSelector = Callable[[list[WaybackRelease]], list[WaybackRelease]]


@dataclass
class _Prepared:
    """A download whose network half is done and whose CPU half is pending.

    Splitting the two lets the pipeline in :meth:`WaybackService.download_many`
    keep the network busy while an earlier image is still being encoded.
    """

    release: WaybackRelease
    request: DownloadRequest
    rendered: "RenderedImage"
    imagery: ImageryMetadata
    image_path: Path
    geotiff_path: Path | None
    metadata_path: Path


@dataclass
class DownloadResult:
    """Everything produced for a single downloaded image."""

    release: WaybackRelease
    rendered: RenderedImage
    metadata: OutputMetadata
    image_path: Path
    metadata_path: Path
    geotiff_path: Path | None = None

    @property
    def image(self) -> Image.Image:
        """The rendered image."""
        return self.rendered.image


class WaybackService:
    """High-level Wayback operations backed by one pooled HTTP client."""

    def __init__(
        self,
        settings: Settings | None = None,
        use_cache: bool = True,
        progress: ProgressReporter | None = None,
    ) -> None:
        """Construct the service and its collaborators."""
        self.settings = settings or get_settings()
        self.settings.ensure_directories()
        self.progress = progress or NullProgress()

        self._cache = CacheStore(
            self.settings.cache_dir,
            size_limit=self.settings.cache_size_limit,
            enabled=use_cache,
        )
        self._http = AsyncHttpClient(self.settings)
        self.catalog = WaybackCatalog(self._http, self.settings, self._cache)
        self.imagery = ImageryService(self._http, self.settings, self._cache)
        self.metadata = MetadataClient(self._http, self.settings, self._cache)

    async def __aenter__(self) -> "WaybackService":
        """Enter the async context, leaving discovery to the first call."""
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

    async def list_versions(
        self,
        coordinate: Coordinate,
        zoom: int,
        only_local_changes: bool = True,
    ) -> list[WaybackRelease]:
        """List the Wayback releases available at a location, newest first."""
        _, all_releases = await self.catalog.load()
        with self.progress.task("Probing releases", total=len(all_releases)) as task:
            return await self.catalog.available_releases(
                coordinate, zoom, only_local_changes, on_progress=lambda: task.advance(1)
            )

    async def resolve_release(
        self,
        coordinate: Coordinate,
        zoom: int,
        target_date: dt.date,
        only_local_changes: bool = True,
    ) -> tuple[WaybackRelease, list[WaybackRelease]]:
        """Select the release closest to a date, returning it and all candidates."""
        candidates = await self.list_versions(coordinate, zoom, only_local_changes)
        return match_nearest_release(candidates, target_date), candidates

    async def download(
        self,
        request: DownloadRequest,
        output_dir: Path | None = None,
        filename_stem: str | None = None,
        write_geotiff: bool = False,
    ) -> DownloadResult:
        """Download the imagery closest to the requested date and write it out."""
        release, _ = await self.resolve_release(
            request.coordinate, request.zoom, request.requested_date, request.only_local_changes
        )
        return await self.download_release(
            release, request, output_dir, filename_stem, write_geotiff
        )

    async def download_release(
        self,
        release: WaybackRelease,
        request: DownloadRequest,
        output_dir: Path | None = None,
        filename_stem: str | None = None,
        write_geotiff: bool = False,
    ) -> DownloadResult:
        """Render one specific release and persist the image plus its sidecar."""
        prepared = await self._fetch(release, request, output_dir, filename_stem, write_geotiff)
        return await self._encode(prepared)

    async def _fetch(
        self,
        release: WaybackRelease,
        request: DownloadRequest,
        output_dir: Path | None,
        filename_stem: str | None,
        write_geotiff: bool,
    ) -> "_Prepared":
        """Do the network half: fetch every tile and the imagery metadata."""
        target_dir = output_dir or self.settings.output_dir
        stem = filename_stem or release.release_date.isoformat()

        rendered = await self._render(release, request)
        imagery_metadata = await self.metadata.fetch(release, request.coordinate, request.zoom)

        return _Prepared(
            release=release,
            request=request,
            rendered=rendered,
            imagery=imagery_metadata,
            image_path=target_dir / f"{stem}.{request.image_format}",
            geotiff_path=target_dir / f"{stem}.tif" if write_geotiff else None,
            metadata_path=target_dir / f"{stem}.json",
        )

    async def _encode(self, prepared: "_Prepared") -> DownloadResult:
        """Do the CPU half: encode and write the outputs.

        Encoding dominates a download -- on a 2048x2048 PNG it outweighs
        stitching roughly 10:1 -- and it is pure CPU work that releases the GIL
        inside Pillow. Running it on a worker thread keeps the event loop free,
        which is what lets :meth:`download_many` overlap one image's encode with
        the next image's downloads.
        """
        geotiff_path = await asyncio.to_thread(
            self._write_outputs,
            prepared.rendered,
            prepared.image_path,
            prepared.geotiff_path,
            prepared.request,
        )

        metadata = self._build_metadata(
            prepared.request, prepared.rendered, prepared.imagery, prepared.image_path
        )
        prepared.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        prepared.metadata_path.write_text(
            json.dumps(metadata.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        logger.info("Wrote %s (%s)", prepared.image_path.name, prepared.rendered.stats.summary())
        return DownloadResult(
            release=prepared.release,
            rendered=prepared.rendered,
            metadata=metadata,
            image_path=prepared.image_path,
            metadata_path=prepared.metadata_path,
            geotiff_path=geotiff_path,
        )

    async def download_many(
        self,
        releases: Sequence[WaybackRelease],
        request: DownloadRequest,
        output_dir: Path | None = None,
        write_geotiff: bool = False,
        name_by_zoom: bool = False,
    ) -> list[DownloadResult]:
        """Download several releases for the same viewport, oldest first.

        Releases are processed sequentially so the concurrency budget applies
        to tiles within one image rather than being split across images, which
        keeps the per-image progress bar meaningful.

        ``name_by_zoom`` appends a ``_z{level}`` suffix to each filename, which
        callers spanning several zoom levels need to keep outputs distinct.
        """
        ordered = sorted(releases, key=lambda item: item.release_date)
        results: list[DownloadResult] = []
        pending: deque[asyncio.Task[DownloadResult]] = deque()

        async def harvest() -> None:
            """Collect the oldest in-flight encode, logging rather than raising."""
            try:
                results.append(await pending.popleft())
            except Exception as exc:
                logger.error("Encoding failed: %s", exc)

        for release in ordered:
            # Bound how many mosaics are alive at once: each holds its full
            # RGBA bitmap, which is ~67 MB at 4096x4096.
            while len(pending) >= self.settings.encode_workers:
                await harvest()

            try:
                prepared = await self._fetch(
                    release,
                    request,
                    output_dir,
                    self._stem(release, request.zoom, name_by_zoom),
                    write_geotiff,
                )
            except Exception as exc:
                logger.error("Skipping %s: %s", release.release_date, exc)
                continue

            pending.append(asyncio.create_task(self._encode(prepared)))

        while pending:
            await harvest()

        if not results:
            raise ImageryUnavailableError("Every release in the selection failed to download.")
        return results

    def _write_outputs(
        self,
        rendered: RenderedImage,
        image_path: Path,
        geotiff_path: Path | None,
        request: DownloadRequest,
    ) -> Path | None:
        """Encode and write the image files. Runs on a worker thread."""
        stitch.save_image(
            rendered.image,
            image_path,
            request.image_format,
            self.settings.jpeg_quality,
            self.settings.png_compress_level,
        )
        if geotiff_path is None:
            return None
        return geotiff.write_geotiff(rendered.image, rendered.bounds, geotiff_path)

    @staticmethod
    def _stem(release: WaybackRelease, zoom: int, name_by_zoom: bool) -> str:
        """Build the output filename stem for one rendered release."""
        date = release.release_date.isoformat()
        return f"{date}_z{zoom}" if name_by_zoom else date

    async def resolve_zoom_levels(self, coordinate: Coordinate, target_date: dt.date) -> list[int]:
        """Discover which zoom levels actually carry imagery at a location.

        Read from the ``MinMapLevel``/``MaxMapLevel`` of the imagery footprints
        covering the point, so the answer reflects what the service publishes
        there rather than a hard-coded guess. Coverage changes over time, so the
        range is taken from the release nearest the requested date.
        """
        try:
            release, _ = await self.resolve_release(coordinate, ZOOM_PROBE_LEVEL, target_date)
        except ImageryUnavailableError:
            # Nothing at the probe level; fall back to the newest release, which
            # still describes the coverage bands available here.
            release = (await self.catalog.all_releases())[0]

        span = await self.metadata.zoom_range(release, coordinate)
        if span is None:
            raise ImageryUnavailableError(
                f"Could not determine which zoom levels carry imagery at {coordinate}. "
                "Pass an explicit span instead, for example --zoom-range 14-19."
            )

        low, high = span
        levels = list(range(low, high + 1))
        logger.info(
            "Imagery at %s spans zoom %d-%d (%d level(s))", coordinate, low, high, len(levels)
        )
        return levels

    async def download_levels(
        self,
        request: DownloadRequest,
        zooms: Sequence[int],
        select: ReleaseSelector | None = None,
        output_dir: Path | None = None,
        write_geotiff: bool = False,
    ) -> dict[int, list[DownloadResult]]:
        """Download a viewport at several zoom levels, grouped by level.

        Every level independently resolves which releases are available to it.
        Local change detection is per-tile, and a tile at zoom 12 covers vastly
        more ground than one at zoom 19, so both the set of dates with imagery
        and the release closest to a requested date genuinely differ between
        levels -- they cannot be resolved once and reused.

        ``select`` chooses which of a level's candidate releases to download;
        it defaults to the single release nearest the requested date. Pass
        :func:`filter_by_date_range` bound to a span, or ``list``, to download
        more. A level whose selector finds nothing is skipped rather than
        failing the whole run.

        Filenames carry a ``_z{level}`` suffix so that two levels resolving to
        the same release date cannot overwrite each other.
        """
        chooser: ReleaseSelector = select or (
            lambda candidates: [match_nearest_release(candidates, request.requested_date)]
        )

        grouped: dict[int, list[DownloadResult]] = {}
        failures: list[str] = []

        for zoom in sorted(set(zooms)):
            leveled = request.model_copy(update={"zoom": zoom})
            try:
                candidates = await self.list_versions(
                    leveled.coordinate, zoom, leveled.only_local_changes
                )
                selected = chooser(candidates)
                if not selected:
                    raise ImageryUnavailableError("no release matched the selection")
                grouped[zoom] = await self.download_many(
                    selected,
                    leveled,
                    output_dir,
                    write_geotiff=write_geotiff,
                    name_by_zoom=True,
                )
            except WaybackError as exc:
                failures.append(f"zoom {zoom}: {exc}")
                logger.error("Skipping zoom %d: %s", zoom, exc)

        if not grouped:
            detail = "\n  ".join(failures) or "no detail captured"
            raise ImageryUnavailableError(
                f"No zoom level could be downloaded at {request.coordinate}:\n  {detail}"
            )
        return grouped

    async def download_bbox_levels(
        self,
        bbox: BoundingBox,
        zooms: Sequence[int],
        target_date: dt.date,
        image_format: str = "png",
        output_dir: Path | None = None,
        only_local_changes: bool = True,
        write_geotiff: bool = False,
    ) -> dict[int, DownloadResult]:
        """Download a bounding box at several zoom levels.

        Unlike a point download, the output size is not fixed: a box covers four
        times as many pixels at each successive level, so each level is planned
        separately and the tile cap applies per level.
        """
        results: dict[int, DownloadResult] = {}
        failures: list[str] = []

        for zoom in sorted(set(zooms)):
            try:
                results[zoom] = await self.download_bbox(
                    bbox,
                    zoom,
                    target_date,
                    image_format=image_format,
                    output_dir=output_dir,
                    only_local_changes=only_local_changes,
                    write_geotiff=write_geotiff,
                    name_by_zoom=True,
                )
            except WaybackError as exc:
                failures.append(f"zoom {zoom}: {exc}")
                logger.error("Skipping zoom %d: %s", zoom, exc)

        if not results:
            detail = "\n  ".join(failures) or "no detail captured"
            raise ImageryUnavailableError(
                f"No zoom level could be downloaded for this bounding box:\n  {detail}"
            )
        return results

    async def download_date_range(
        self,
        request: DownloadRequest,
        start: dt.date,
        end: dt.date,
        output_dir: Path | None = None,
        write_geotiff: bool = False,
    ) -> list[DownloadResult]:
        """Download every available release inside an inclusive date range."""
        candidates = await self.list_versions(
            request.coordinate, request.zoom, request.only_local_changes
        )
        selected = filter_by_date_range(candidates, start, end)
        return await self.download_many(selected, request, output_dir, write_geotiff)

    async def download_all(
        self,
        request: DownloadRequest,
        output_dir: Path | None = None,
        write_geotiff: bool = False,
    ) -> list[DownloadResult]:
        """Download every release that shows a change at this location."""
        candidates = await self.list_versions(
            request.coordinate, request.zoom, request.only_local_changes
        )
        return await self.download_many(candidates, request, output_dir, write_geotiff)

    async def download_bbox(
        self,
        bbox: BoundingBox,
        zoom: int,
        target_date: dt.date,
        image_format: str = "png",
        output_dir: Path | None = None,
        only_local_changes: bool = True,
        write_geotiff: bool = False,
        name_by_zoom: bool = False,
    ) -> DownloadResult:
        """Download the imagery covering a bounding box at a given date."""
        center = bbox.center
        release, _ = await self.resolve_release(center, zoom, target_date, only_local_changes)

        grid = await self._plan_bbox(bbox, zoom)
        request = DownloadRequest(
            coordinate=center,
            requested_date=target_date,
            zoom=zoom,
            width=grid.crop_box[2] - grid.crop_box[0],
            height=grid.crop_box[3] - grid.crop_box[1],
            image_format=image_format,  # type: ignore[arg-type]
            only_local_changes=only_local_changes,
        )
        return await self.download_release(
            release,
            request,
            output_dir,
            filename_stem=self._stem(release, zoom, name_by_zoom),
            write_geotiff=write_geotiff,
        )

    def build_timelapse(
        self,
        results: Iterable[DownloadResult],
        output_dir: Path,
        stem: str = "timelapse",
        fps: float = 2.0,
        make_gif: bool = True,
        make_mp4: bool = False,
        label_frames: bool = True,
    ) -> list[Path]:
        """Assemble downloaded images into a GIF and/or MP4 time-lapse."""
        ordered = sorted(results, key=lambda item: item.release.release_date)
        frames = [
            (
                animation.annotate(result.image, result.release.release_date.isoformat())
                if label_frames
                else result.image
            )
            for result in ordered
        ]

        written: list[Path] = []
        if make_gif:
            written.append(animation.write_gif(frames, output_dir / f"{stem}.gif", fps=fps))
        if make_mp4:
            written.append(animation.write_mp4(frames, output_dir / f"{stem}.mp4", fps=fps))
        return written

    async def _render(self, release: WaybackRelease, request: DownloadRequest) -> RenderedImage:
        """Render a request through the imagery service with a progress bar."""
        grid = self.imagery.plan(request.coordinate, request.zoom, request.width, request.height)
        label = f"Tiles {release.release_date.isoformat()}"
        with self.progress.task(label, total=grid.tile_count) as task:
            return await self.imagery.render(
                release,
                request.coordinate,
                request.zoom,
                request.width,
                request.height,
                on_progress=task.advance,
            )

    async def _plan_bbox(self, bbox: BoundingBox, zoom: int):
        """Plan the tile grid for a bounding box."""
        from wayback_downloader.gis.tiles import plan_grid_for_bbox

        return plan_grid_for_bbox(bbox, zoom, self.settings.tile_size)

    def _build_metadata(
        self,
        request: DownloadRequest,
        rendered: RenderedImage,
        imagery: ImageryMetadata,
        image_path: Path,
    ) -> OutputMetadata:
        """Assemble the JSON sidecar describing one produced image."""
        release = rendered.release
        return OutputMetadata(
            latitude=request.coordinate.latitude,
            longitude=request.coordinate.longitude,
            requested_date=request.requested_date,
            matched_date=release.release_date,
            date_offset_days=(release.release_date - request.requested_date).days,
            zoom=request.zoom,
            layer_id=release.item_id,
            release_num=release.release_num,
            layer_identifier=release.layer_identifier,
            service_url=self.catalog.endpoints.tile_service_base,
            tile_url_template=release.tile_url_template,
            metadata_service_url=release.metadata_url,
            imagery_provider=imagery.provider,
            imagery_product=imagery.product,
            imagery_sensor=imagery.sensor,
            imagery_acquisition_date=imagery.acquisition_date,
            resolution=describe_resolution(
                request.coordinate, request.zoom, self.settings.tile_size
            ),
            ground_resolution_m_per_px=round(rendered.ground_resolution_m, 6),
            source_resolution_m=imagery.source_resolution_m,
            tile_count=rendered.stats.requested,
            tile_grid={
                "z": rendered.grid.z,
                "min_x": rendered.grid.min_x,
                "min_y": rendered.grid.min_y,
                "max_x": rendered.grid.max_x,
                "max_y": rendered.grid.max_y,
                "columns": rendered.grid.columns,
                "rows": rendered.grid.rows,
            },
            bounds_wgs84={
                "west": round(rendered.bounds.west, 8),
                "south": round(rendered.bounds.south, 8),
                "east": round(rendered.bounds.east, 8),
                "north": round(rendered.bounds.north, 8),
            },
            image_size={"width": rendered.image.width, "height": rendered.image.height},
            image_file=image_path.name,
            generated_at=dt.datetime.now(dt.timezone.utc),
            tool_version=__version__,
        )
