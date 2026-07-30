"""Typer command-line interface."""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
from pathlib import Path
from typing import Annotated, Any, Callable, Optional, TypeVar

import typer
from rich.table import Table

from wayback_downloader import __version__
from wayback_downloader.api.wayback import filter_by_date_range
from wayback_downloader.config import get_settings
from wayback_downloader.exceptions import ImageryUnavailableError, WaybackError
from wayback_downloader.models import Coordinate, DownloadRequest, TileIndex
from wayback_downloader.service import DownloadResult, WaybackService
from wayback_downloader.utils.cache import CacheStore
from wayback_downloader.utils.inputs import read_batch
from wayback_downloader.utils.logger import configure_logging, console, error_console, print_kv
from wayback_downloader.utils.progress import NullProgress, RichProgress
from wayback_downloader.utils.validator import (
    parse_size,
    parse_zoom_levels,
    validate_bbox,
    validate_coordinate,
    validate_date,
    validate_date_range,
    validate_zoom,
)

app = typer.Typer(
    name="wayback",
    help="Download historical satellite imagery from the Esri Living Atlas World Imagery "
    "Wayback archive.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)

LatOption = Annotated[float, typer.Option("--lat", help="Latitude in WGS84 degrees.")]
LonOption = Annotated[float, typer.Option("--lon", help="Longitude in WGS84 degrees.")]
ZoomOption = Annotated[int, typer.Option("--zoom", "-z", help="Tile zoom level (0-23).")]
SizeOption = Annotated[
    str, typer.Option("--size", "-s", help="Output size in pixels: 1024 or 1024x768.")
]
FormatOption = Annotated[str, typer.Option("--format", "-f", help="Image format: png or jpg.")]
OutputOption = Annotated[Optional[Path], typer.Option("--output", "-o", help="Output directory.")]
ZoomRangeOption = Annotated[
    Optional[str],
    typer.Option(
        "--zoom-range",
        help="Work across several zoom levels instead of one: a span (14-19), a list "
        "(12,15,18), or 'all' for every level with imagery at this location. "
        "Overrides --zoom; outputs gain a _z{level} suffix.",
    ),
]

_STATE: dict[str, bool] = {"verbose": False, "quiet": False, "cache": True}

# Root tile, used only to render an example tilemap URL in `endpoints`.
_SAMPLE_TILE = TileIndex(z=0, x=0, y=0)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show debug logging.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only report errors.")] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Bypass the on-disk cache.")] = False,
    version: Annotated[bool, typer.Option("--version", help="Print the version and exit.")] = False,
) -> None:
    """Configure global options shared by every command.

    Declared ``invoke_without_command`` so that ``--version`` works on its own;
    otherwise the group would reject the call for having no subcommand before
    this ever ran.
    """
    if version:
        console.print(f"wayback-downloader {__version__}")
        raise typer.Exit()

    configure_logging(verbose=verbose, quiet=quiet)
    _STATE.update(verbose=verbose, quiet=quiet, cache=not no_cache)

    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


def _make_service() -> WaybackService:
    """Build a service honouring the global CLI flags."""
    progress = NullProgress() if _STATE["quiet"] else RichProgress()
    return WaybackService(use_cache=_STATE["cache"], progress=progress)


F = TypeVar("F", bound=Callable[..., Any])


def handle_errors(command: F) -> F:
    """Map domain errors raised anywhere in a command onto CLI exit codes.

    Wrapping the whole command body -- not just the async part -- means
    argument validation failures are reported the same way as network ones,
    with their own exit code instead of an unhandled traceback.
    """

    @functools.wraps(command)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return command(*args, **kwargs)
        except WaybackError as exc:
            error_console.print(f"[error]Error:[/error] {exc}")
            raise typer.Exit(code=exc.exit_code) from None
        except KeyboardInterrupt:
            error_console.print("[warn]Interrupted.[/warn]")
            raise typer.Exit(code=130) from None

    return wrapper  # type: ignore[return-value]


def _run(coroutine: Any) -> None:
    """Run a command coroutine on a fresh event loop."""
    asyncio.run(coroutine)


def _report(result: DownloadResult) -> None:
    """Print a summary of one completed download."""
    metadata = result.metadata
    console.print(f"\n[success]Saved[/success] {result.image_path}")
    print_kv("Requested date", metadata.requested_date.isoformat())
    print_kv(
        "Matched release",
        f"{metadata.matched_date.isoformat()} ({metadata.date_offset_days:+d} days)",
    )
    print_kv("Release number", metadata.release_num)
    print_kv("Layer id", metadata.layer_id)
    print_kv("Imagery provider", metadata.imagery_provider or "unknown")
    print_kv("Acquisition date", metadata.imagery_acquisition_date or "unknown")
    print_kv("Resolution", metadata.resolution)
    print_kv("Tiles", metadata.tile_count)
    print_kv("Image size", f"{metadata.image_size['width']}x{metadata.image_size['height']}")
    print_kv("Metadata", result.metadata_path.name)
    if result.geotiff_path:
        print_kv("GeoTIFF", result.geotiff_path.name)


async def _effective_levels(
    service: WaybackService,
    zoom_range: str | None,
    zoom: int,
    coordinate: Coordinate,
    reference_date: dt.date,
) -> list[int]:
    """Resolve the zoom levels a command should operate on.

    Without ``--zoom-range`` this is just ``[--zoom]``, so every command keeps
    its single-level behaviour untouched. With ``all`` the levels are discovered
    from the imagery footprints covering the point.
    """
    if zoom_range is None:
        return [validate_zoom(zoom)]

    levels = parse_zoom_levels(zoom_range)
    if levels is None:
        levels = await service.resolve_zoom_levels(coordinate, reference_date)

    console.print(
        f"[info]Zoom levels:[/info] {', '.join(str(level) for level in levels)} "
        f"[muted]({len(levels)} level(s))[/muted]"
    )
    return levels


def _flatten(grouped: dict[int, list[DownloadResult]]) -> list[DownloadResult]:
    """Flatten per-zoom results into one list, ordered by zoom then date."""
    return [result for zoom in sorted(grouped) for result in grouped[zoom]]


def _report_multi_zoom(grouped: dict[int, list[DownloadResult]], target: Path) -> None:
    """Summarise a multi-level, multi-release download."""
    total = sum(len(results) for results in grouped.values())
    table = Table(
        title=f"Downloaded {total} image(s) across {len(grouped)} zoom level(s) to {target}"
    )
    table.add_column("Zoom", justify="right", style="cyan")
    table.add_column("Images", justify="right")
    table.add_column("Resolution", justify="right")
    table.add_column("Dates", style="dim")

    for zoom in sorted(grouped):
        results = grouped[zoom]
        dates = [result.release.release_date.isoformat() for result in results]
        shown = ", ".join(dates[:4]) + (f" (+{len(dates) - 4} more)" if len(dates) > 4 else "")
        table.add_row(str(zoom), str(len(results)), results[0].metadata.resolution, shown)
    console.print(table)


def _animate(
    service: WaybackService,
    grouped: dict[int, list[DownloadResult]],
    target: Path,
    fps: float,
    make_gif: bool,
    make_mp4: bool,
    stem: str = "timelapse",
) -> None:
    """Build one animation per zoom level, if any was requested.

    Levels are never mixed into a single animation: frames at different zooms
    show different ground extents, so interleaving them would read as the
    camera jumping rather than as change over time.
    """
    if not (make_gif or make_mp4):
        return

    multi = len(grouped) > 1
    for zoom in sorted(grouped):
        results = grouped[zoom]
        if len(results) < 2:
            error_console.print(
                f"[warn]zoom {zoom}:[/warn] only {len(results)} frame(s), skipping animation."
            )
            continue
        level_stem = f"{stem}_z{zoom}" if multi else stem
        for path in service.build_timelapse(
            results, target, stem=level_stem, fps=fps, make_gif=make_gif, make_mp4=make_mp4
        ):
            console.print(f"[success]Animation:[/success] {path}")


def _report_zoom_levels(results: list[DownloadResult], target: Path) -> None:
    """Print a per-zoom summary of a multi-level download."""
    table = Table(title=f"Downloaded {len(results)} zoom level(s) to {target}")
    table.add_column("Zoom", justify="right", style="cyan")
    table.add_column("Matched date")
    table.add_column("Offset", justify="right", style="dim")
    table.add_column("Resolution", justify="right")
    table.add_column("Tiles", justify="right", style="dim")
    table.add_column("File", style="dim")

    for result in results:
        metadata = result.metadata
        table.add_row(
            str(metadata.zoom),
            metadata.matched_date.isoformat(),
            f"{metadata.date_offset_days:+d}d",
            metadata.resolution,
            str(metadata.tile_count),
            result.image_path.name,
        )
    console.print(table)


def _build_request(
    lat: float,
    lon: float,
    date: str,
    zoom: int,
    size: str,
    image_format: str,
    all_versions: bool = False,
) -> DownloadRequest:
    """Validate raw CLI arguments into a domain request object."""
    coordinate = validate_coordinate(lat, lon)
    width, height = parse_size(size)
    return DownloadRequest(
        coordinate=coordinate,
        requested_date=validate_date(date),
        zoom=validate_zoom(zoom),
        width=width,
        height=height,
        image_format=image_format.lower().lstrip("."),  # type: ignore[arg-type]
        only_local_changes=not all_versions,
    )


@app.command()
@handle_errors
def download(
    lat: LatOption,
    lon: LonOption,
    date: Annotated[str, typer.Option("--date", "-d", help="Target date, YYYY-MM-DD.")],
    zoom: ZoomOption = 17,
    zoom_range: ZoomRangeOption = None,
    size: SizeOption = "1024",
    image_format: FormatOption = "png",
    output: OutputOption = None,
    geotiff: Annotated[
        bool, typer.Option("--geotiff", help="Also write a georeferenced GeoTIFF.")
    ] = False,
    all_versions: Annotated[
        bool,
        typer.Option("--all-versions", help="Consider every release, not only local changes."),
    ] = False,
) -> None:
    """Download the imagery closest to a date at a coordinate."""
    request = _build_request(lat, lon, date, zoom, size, image_format, all_versions)

    async def run() -> None:
        async with _make_service() as service:
            if zoom_range is None:
                result = await service.download(request, output_dir=output, write_geotiff=geotiff)
                _report(result)
                return

            levels = await _effective_levels(
                service, zoom_range, zoom, request.coordinate, request.requested_date
            )
            grouped = await service.download_levels(
                request, levels, output_dir=output, write_geotiff=geotiff
            )
            _report_zoom_levels(_flatten(grouped), output or service.settings.output_dir)

    _run(run())


@app.command()
@handle_errors
def versions(
    lat: LatOption,
    lon: LonOption,
    zoom: ZoomOption = 17,
    zoom_range: ZoomRangeOption = None,
    all_versions: Annotated[
        bool,
        typer.Option("--all-versions", help="List every release, not only local changes."),
    ] = False,
) -> None:
    """List the Wayback releases available at a coordinate."""
    coordinate = validate_coordinate(lat, lon)

    async def run() -> None:
        async with _make_service() as service:
            levels = await _effective_levels(service, zoom_range, zoom, coordinate, dt.date.today())

            if len(levels) == 1:
                releases = await service.list_versions(coordinate, levels[0], not all_versions)
                table = Table(title=f"Wayback releases at {coordinate} (zoom {levels[0]})")
                table.add_column("Date", style="cyan")
                table.add_column("Release", justify="right")
                table.add_column("Layer", style="dim")
                table.add_column("Item ID", style="dim")
                for release in releases:
                    table.add_row(
                        release.release_date.isoformat(),
                        str(release.release_num),
                        release.layer_identifier or "-",
                        release.item_id,
                    )
                console.print(table)
                console.print(f"[muted]{len(releases)} release(s) with imagery here.[/muted]")
                return

            # Across several levels the interesting fact is which dates exist at
            # which level, so render a date-by-zoom matrix rather than N lists.
            per_level: dict[int, set[dt.date]] = {}
            for level in levels:
                try:
                    releases = await service.list_versions(coordinate, level, not all_versions)
                    per_level[level] = {release.release_date for release in releases}
                except WaybackError as exc:
                    error_console.print(f"[warn]zoom {level}:[/warn] {exc}")

            if not per_level:
                raise ImageryUnavailableError(
                    f"No Wayback imagery is available at {coordinate} at any requested zoom."
                )

            every_date = sorted({date for dates in per_level.values() for date in dates})
            table = Table(title=f"Release dates by zoom level at {coordinate}")
            table.add_column("Date", style="cyan")
            for level in sorted(per_level):
                table.add_column(str(level), justify="center")

            for date in every_date:
                table.add_row(
                    date.isoformat(),
                    *(
                        "[success]*[/success]" if date in per_level[level] else "[muted]-[/muted]"
                        for level in sorted(per_level)
                    ),
                )
            console.print(table)
            console.print(
                f"[muted]{len(every_date)} distinct date(s); "
                f"a '*' marks the levels where that release carries imagery.[/muted]"
            )

    _run(run())


@app.command(name="range")
@handle_errors
def date_range(
    lat: LatOption,
    lon: LonOption,
    start: Annotated[str, typer.Option("--start", help="Range start date, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option("--end", help="Range end date, YYYY-MM-DD.")],
    zoom: ZoomOption = 17,
    zoom_range: ZoomRangeOption = None,
    size: SizeOption = "1024",
    image_format: FormatOption = "png",
    output: OutputOption = None,
    gif: Annotated[bool, typer.Option("--gif", help="Also build an animated GIF.")] = False,
    mp4: Annotated[bool, typer.Option("--mp4", help="Also build an MP4 time-lapse.")] = False,
    fps: Annotated[float, typer.Option("--fps", help="Animation frames per second.")] = 2.0,
) -> None:
    """Download every release between two dates at a coordinate."""
    start_date, end_date = validate_date_range(validate_date(start), validate_date(end))
    request = _build_request(lat, lon, start, zoom, size, image_format)

    async def run() -> None:
        async with _make_service() as service:
            target = output or service.settings.output_dir

            if zoom_range is None:
                results = await service.download_date_range(
                    request, start_date, end_date, output_dir=output
                )
                console.print(
                    f"\n[success]Downloaded {len(results)} image(s)[/success] to {target}"
                )
                for result in results:
                    console.print(f"  [muted]{result.image_path.name}[/muted]")
                _animate(service, {request.zoom: results}, target, fps, gif, mp4)
                return

            levels = await _effective_levels(
                service, zoom_range, zoom, request.coordinate, start_date
            )
            grouped = await service.download_levels(
                request,
                levels,
                select=lambda candidates: filter_by_date_range(candidates, start_date, end_date),
                output_dir=output,
            )
            _report_multi_zoom(grouped, target)
            _animate(service, grouped, target, fps, gif, mp4)

    _run(run())


@app.command(name="all")
@handle_errors
def download_all(
    lat: LatOption,
    lon: LonOption,
    zoom: ZoomOption = 17,
    zoom_range: ZoomRangeOption = None,
    size: SizeOption = "1024",
    image_format: FormatOption = "png",
    output: OutputOption = None,
    gif: Annotated[bool, typer.Option("--gif", help="Also build an animated GIF.")] = False,
    mp4: Annotated[bool, typer.Option("--mp4", help="Also build an MP4 time-lapse.")] = False,
    fps: Annotated[float, typer.Option("--fps", help="Animation frames per second.")] = 2.0,
) -> None:
    """Download every release that shows a change at a coordinate."""
    request = _build_request(lat, lon, dt.date.today().isoformat(), zoom, size, image_format)

    async def run() -> None:
        async with _make_service() as service:
            target = output or service.settings.output_dir

            if zoom_range is None:
                results = await service.download_all(request, output_dir=output)
                console.print(
                    f"\n[success]Downloaded {len(results)} image(s)[/success] to {target}"
                )
                _animate(service, {request.zoom: results}, target, fps, gif, mp4)
                return

            levels = await _effective_levels(
                service, zoom_range, zoom, request.coordinate, request.requested_date
            )
            grouped = await service.download_levels(request, levels, select=list, output_dir=output)
            _report_multi_zoom(grouped, target)
            _animate(service, grouped, target, fps, gif, mp4)

    _run(run())


@app.command()
@handle_errors
def timelapse(
    lat: LatOption,
    lon: LonOption,
    zoom: ZoomOption = 17,
    zoom_range: ZoomRangeOption = None,
    size: SizeOption = "1024",
    output: OutputOption = None,
    start: Annotated[
        Optional[str], typer.Option("--start", help="Restrict to releases from this date.")
    ] = None,
    end: Annotated[
        Optional[str], typer.Option("--end", help="Restrict to releases up to this date.")
    ] = None,
    fps: Annotated[float, typer.Option("--fps", help="Animation frames per second.")] = 2.0,
    mp4: Annotated[
        bool, typer.Option("--mp4", help="Also write an MP4 alongside the GIF.")
    ] = False,
) -> None:
    """Build a date-stamped GIF (and optionally MP4) of every change at a coordinate.

    With --zoom-range, one animation is produced per zoom level.
    """
    request = _build_request(lat, lon, dt.date.today().isoformat(), zoom, size, "png")
    start_date = validate_date(start) if start else None
    end_date = validate_date(end) if end else None
    if start_date and end_date:
        validate_date_range(start_date, end_date)

    def select(candidates: list) -> list:
        """Restrict a level's candidate releases to the requested window, if any."""
        if start_date and end_date:
            return filter_by_date_range(candidates, start_date, end_date)
        return list(candidates)

    async def run() -> None:
        async with _make_service() as service:
            target = output or service.settings.output_dir
            levels = await _effective_levels(
                service, zoom_range, zoom, request.coordinate, request.requested_date
            )
            grouped = await service.download_levels(
                request, levels, select=select, output_dir=target
            )

            stem = f"timelapse_{request.coordinate.latitude:.5f}_{request.coordinate.longitude:.5f}"
            _animate(service, grouped, target, fps, make_gif=True, make_mp4=mp4, stem=stem)
            total = sum(len(results) for results in grouped.values())
            console.print(f"[muted]{total} frame(s) across {len(grouped)} zoom level(s).[/muted]")

    _run(run())


@app.command()
@handle_errors
def bbox(
    west: Annotated[float, typer.Option("--west", help="Western longitude.")],
    south: Annotated[float, typer.Option("--south", help="Southern latitude.")],
    east: Annotated[float, typer.Option("--east", help="Eastern longitude.")],
    north: Annotated[float, typer.Option("--north", help="Northern latitude.")],
    date: Annotated[str, typer.Option("--date", "-d", help="Target date, YYYY-MM-DD.")],
    zoom: ZoomOption = 16,
    zoom_range: ZoomRangeOption = None,
    image_format: FormatOption = "png",
    output: OutputOption = None,
    geotiff: Annotated[
        bool, typer.Option("--geotiff", help="Also write a georeferenced GeoTIFF.")
    ] = False,
) -> None:
    """Download the imagery covering a bounding box.

    With --zoom-range the box is rendered at each level; note that the output
    grows fourfold per level, so a wide span can hit the per-request tile cap
    at the top end.
    """
    box = validate_bbox(west, south, east, north)
    target_date = validate_date(date)
    normalized_format = image_format.lower().lstrip(".")

    async def run() -> None:
        async with _make_service() as service:
            if zoom_range is None:
                result = await service.download_bbox(
                    box,
                    validate_zoom(zoom),
                    target_date,
                    image_format=normalized_format,
                    output_dir=output,
                    write_geotiff=geotiff,
                )
                _report(result)
                return

            levels = await _effective_levels(service, zoom_range, zoom, box.center, target_date)
            results = await service.download_bbox_levels(
                box,
                levels,
                target_date,
                image_format=normalized_format,
                output_dir=output,
                write_geotiff=geotiff,
            )
            _report_zoom_levels(
                [results[zoom_level] for zoom_level in sorted(results)],
                output or service.settings.output_dir,
            )

    _run(run())


@app.command()
@handle_errors
def batch(
    file: Annotated[Path, typer.Argument(help="CSV or GeoJSON file of coordinates.")],
    date: Annotated[
        Optional[str],
        typer.Option("--date", "-d", help="Default date for rows that do not specify one."),
    ] = None,
    zoom: ZoomOption = 17,
    zoom_range: ZoomRangeOption = None,
    size: SizeOption = "1024",
    image_format: FormatOption = "png",
    output: OutputOption = None,
) -> None:
    """Download imagery for every coordinate in a CSV or GeoJSON file.

    A --zoom-range applies to every row and overrides any per-row zoom column.
    """
    entries = read_batch(file)
    default_date = validate_date(date) if date else dt.date.today()
    width, height = parse_size(size)

    async def run() -> None:
        async with _make_service() as service:
            root = output or service.settings.output_dir
            succeeded = 0

            for entry in entries:
                request = DownloadRequest(
                    coordinate=entry.coordinate,
                    requested_date=entry.date or default_date,
                    zoom=validate_zoom(entry.zoom or zoom),
                    width=width,
                    height=height,
                    image_format=image_format.lower().lstrip("."),  # type: ignore[arg-type]
                )
                console.print(f"[info]{entry.name}[/info] {entry.coordinate}")
                try:
                    if zoom_range is None:
                        result = await service.download(request, output_dir=root / entry.name)
                        console.print(f"  [success]->[/success] {result.image_path}")
                    else:
                        levels = await _effective_levels(
                            service,
                            zoom_range,
                            request.zoom,
                            entry.coordinate,
                            request.requested_date,
                        )
                        grouped = await service.download_levels(
                            request, levels, output_dir=root / entry.name
                        )
                        console.print(
                            f"  [success]->[/success] {sum(len(r) for r in grouped.values())} "
                            f"image(s) across {len(grouped)} level(s) in {root / entry.name}"
                        )
                    succeeded += 1
                except WaybackError as exc:
                    error_console.print(f"  [error]failed:[/error] {exc}")

            console.print(
                f"\n[success]{succeeded}/{len(entries)} coordinate(s) downloaded.[/success]"
            )

    _run(run())


@app.command()
@handle_errors
def endpoints() -> None:
    """Show the REST endpoints discovered at runtime."""

    async def run() -> None:
        async with _make_service() as service:
            discovered, releases = await service.catalog.load()
            console.print("[success]Discovered Wayback endpoints[/success]")
            print_kv("Config document", discovered.config_url)
            print_kv("Tile service base", discovered.tile_service_base)
            print_kv("Tile URL template", releases[0].tile_url_template)
            print_kv("Tilemap probe", discovered.tilemap_url(releases[0].release_num, _SAMPLE_TILE))
            print_kv("Metadata service", releases[0].metadata_url or "none")
            print_kv("Releases", discovered.release_count)
            print_kv(
                "Date span",
                f"{releases[-1].release_date.isoformat()} .. "
                f"{releases[0].release_date.isoformat()}",
            )

    _run(run())


@app.command()
@handle_errors
def cache(
    clear: Annotated[bool, typer.Option("--clear", help="Delete every cached entry.")] = False,
) -> None:
    """Inspect or clear the on-disk cache."""
    settings = get_settings()
    with CacheStore(settings.cache_dir, size_limit=settings.cache_size_limit) as store:
        if clear:
            removed = store.clear()
            console.print(f"[success]Cleared {removed} cache entries.[/success]")
        else:
            print_kv("Cache directory", settings.cache_dir)
            print_kv("Size on disk", f"{store.size_bytes / 1024 / 1024:.1f} MiB")


if __name__ == "__main__":
    app()
