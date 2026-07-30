"""Time-lapse output.

GIF is produced with Pillow, which is always installed. MP4 needs ``imageio``
with an ffmpeg backend and reports a clear install hint when that is missing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw

from wayback_downloader.exceptions import ExportError
from wayback_downloader.utils.logger import get_logger

logger = get_logger(__name__)

_LABEL_MARGIN = 12
_LABEL_PADDING = 6


def annotate(image: Image.Image, label: str) -> Image.Image:
    """Stamp a caption into the bottom-left corner of a copy of the image.

    Uses Pillow's built-in bitmap font so no font file has to ship with the
    package.
    """
    canvas = image.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)

    try:
        box = draw.textbbox((0, 0), label)
    except AttributeError:  # pragma: no cover - very old Pillow
        box = (0, 0, len(label) * 6, 11)
    text_width, text_height = box[2] - box[0], box[3] - box[1]

    x = _LABEL_MARGIN
    y = canvas.height - text_height - _LABEL_MARGIN - 2 * _LABEL_PADDING
    draw.rectangle(
        (
            x - _LABEL_PADDING,
            y - _LABEL_PADDING,
            x + text_width + _LABEL_PADDING,
            y + text_height + _LABEL_PADDING,
        ),
        fill=(0, 0, 0),
    )
    draw.text((x, y), label, fill=(255, 255, 255))
    return canvas


def _normalize(frames: Sequence[Image.Image]) -> list[Image.Image]:
    """Convert frames to RGB and force them all to the first frame's size."""
    if not frames:
        raise ExportError("Cannot build an animation from zero frames.")
    target = frames[0].size
    return [
        frame.convert("RGB") if frame.size == target else frame.convert("RGB").resize(target)
        for frame in frames
    ]


def write_gif(
    frames: Sequence[Image.Image],
    path: Path,
    fps: float = 2.0,
    loop: bool = True,
) -> Path:
    """Write an animated GIF from a sequence of equally sized frames."""
    normalized = _normalize(frames)
    path.parent.mkdir(parents=True, exist_ok=True)

    normalized[0].save(
        path,
        format="GIF",
        save_all=True,
        append_images=normalized[1:],
        duration=int(1000 / max(fps, 0.1)),
        loop=0 if loop else 1,
        optimize=True,
    )
    return path


def write_mp4(frames: Sequence[Image.Image], path: Path, fps: float = 2.0) -> Path:
    """Write an H.264 MP4 time-lapse from a sequence of frames."""
    try:
        import imageio.v3 as iio
        import numpy as np
    except ImportError as exc:
        raise ExportError(
            "MP4 output needs imageio with an ffmpeg backend. "
            "Install it with: pip install 'imageio[ffmpeg]'"
        ) from exc

    normalized = _normalize(frames)
    # yuv420p subsamples chroma 2x1, so both dimensions must be even.
    width = normalized[0].width - (normalized[0].width % 2)
    height = normalized[0].height - (normalized[0].height % 2)
    if (width, height) != normalized[0].size:
        normalized = [frame.crop((0, 0, width, height)) for frame in normalized]

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        iio.imwrite(
            path,
            np.stack([np.asarray(frame) for frame in normalized]),
            # Pin the plugin so the encode path does not change if pyav happens
            # to be installed alongside imageio-ffmpeg.
            plugin="FFMPEG",
            fps=fps,
            codec="libx264",
            # Set the pixel format through the plugin rather than output_params;
            # passing it both ways makes ffmpeg warn about a duplicate option.
            pixelformat="yuv420p",
            # Without this the plugin silently rescales to a multiple of 16.
            # The even-dimension crop above is all yuv420p actually requires.
            macro_block_size=1,
        )
    except Exception as exc:
        raise ExportError(f"Could not encode MP4 to {path}: {exc}") from exc
    return path
