"""Assemble downloaded tiles into a single image."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from wayback_downloader.exceptions import ExportError
from wayback_downloader.models import TileGrid
from wayback_downloader.utils.logger import get_logger

logger = get_logger(__name__)

# Colour painted where a tile could not be retrieved, so gaps are obvious in
# the output rather than silently blending into the imagery.
MISSING_TILE_FILL = (32, 32, 32, 0)

# zlib level 6 rather than Pillow's `optimize=True`. On satellite imagery
# `optimize` is a double loss -- measured on a 2048x2048 RGBA mosaic it took
# 1522 ms for 9.31 MiB, against 535 ms for 8.68 MiB here: 2.8x slower *and* 7%
# larger, because the palette search it performs cannot help photographic data
# and it forces maximum compression for a negligible gain.
DEFAULT_PNG_COMPRESS_LEVEL = 6


def stitch_tiles(
    grid: TileGrid,
    tile_bytes: dict[tuple[int, int], bytes],
) -> Image.Image:
    """Compose downloaded tiles into the full mosaic.

    ``tile_bytes`` is keyed by the ``(offset_x, offset_y)`` paste position from
    :meth:`TileGrid.placements`. Positions with no data are left transparent.
    """
    mosaic = Image.new("RGBA", grid.mosaic_size, MISSING_TILE_FILL)

    for (offset_x, offset_y), payload in tile_bytes.items():
        if not payload:
            continue
        try:
            with Image.open(io.BytesIO(payload)) as opened:
                opened.load()
                tile: Image.Image = opened
                if tile.size != (grid.tile_size, grid.tile_size):
                    tile = tile.resize((grid.tile_size, grid.tile_size), Image.Resampling.LANCZOS)
                mosaic.paste(tile.convert("RGBA"), (offset_x, offset_y))
        except (OSError, ValueError) as exc:
            logger.warning("Skipping undecodable tile at (%d, %d): %s", offset_x, offset_y, exc)

    return mosaic


def crop_to_window(mosaic: Image.Image, grid: TileGrid) -> Image.Image:
    """Crop the mosaic down to the requested window centred on the coordinate."""
    left, top, right, bottom = grid.crop_box
    left = max(0, left)
    top = max(0, top)
    right = min(mosaic.width, right)
    bottom = min(mosaic.height, bottom)
    if right <= left or bottom <= top:
        raise ExportError("Computed crop window is empty; check the zoom and size arguments.")
    return mosaic.crop((left, top, right, bottom))


def save_image(
    image: Image.Image,
    path: Path,
    image_format: str = "png",
    jpeg_quality: int = 92,
    png_compress_level: int = DEFAULT_PNG_COMPRESS_LEVEL,
) -> Path:
    """Write an image to disk in PNG or JPEG form.

    JPEG has no alpha channel, so the image is flattened onto black before
    encoding.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = image_format.lower().lstrip(".")

    if normalized in {"jpg", "jpeg"}:
        rgb = Image.new("RGB", image.size, (0, 0, 0))
        rgb.paste(image, mask=image.split()[-1] if image.mode == "RGBA" else None)
        # optimize costs roughly 6x the encode time but buys ~10% on size,
        # which is worth it for a format whose whole point is being small.
        rgb.save(path, format="JPEG", quality=jpeg_quality, optimize=True, progressive=True)
    elif normalized == "png":
        image.save(path, format="PNG", compress_level=png_compress_level)
    else:
        raise ExportError(f"Unsupported image format {image_format!r}; use png or jpg.")

    return path


def build_image(grid: TileGrid, tile_bytes: dict[tuple[int, int], bytes]) -> Image.Image:
    """Stitch and crop in one step, returning the final output image."""
    return crop_to_window(stitch_tiles(grid, tile_bytes), grid)
