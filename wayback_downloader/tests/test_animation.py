"""Tests for GIF and MP4 time-lapse output."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from wayback_downloader.exceptions import ExportError
from wayback_downloader.export.animation import annotate, write_gif, write_mp4


def has_ffmpeg() -> bool:
    """Report whether an imageio ffmpeg backend is available."""
    try:
        import imageio_ffmpeg  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


requires_ffmpeg = pytest.mark.skipif(not has_ffmpeg(), reason="imageio[ffmpeg] is not installed")


def make_frames(count: int = 4, size: tuple[int, int] = (64, 48)) -> list[Image.Image]:
    """Build a sequence of visually distinct frames."""
    return [Image.new("RGB", size, (index * 40 % 256, 90, 180)) for index in range(count)]


def test_annotate_stamps_without_resizing() -> None:
    """The date caption is drawn onto the frame without changing its size."""
    frame = Image.new("RGB", (120, 90), (200, 200, 200))
    labelled = annotate(frame, "2023-03-15")

    assert labelled.size == frame.size
    # The caption sits on a black band in the bottom-left corner.
    assert labelled.getpixel((14, 68)) != frame.getpixel((14, 68))
    # The top of the frame is untouched.
    assert labelled.getpixel((60, 5)) == frame.getpixel((60, 5))


def test_annotate_leaves_the_original_untouched() -> None:
    """Annotation works on a copy, so callers keep their unlabelled image."""
    frame = Image.new("RGB", (80, 60), (10, 20, 30))
    annotate(frame, "2020-01-01")
    assert frame.getpixel((14, 44)) == (10, 20, 30)


def test_gif_has_one_frame_per_input(tmp_path: Path) -> None:
    """The GIF contains exactly the frames it was given."""
    path = write_gif(make_frames(5), tmp_path / "out.gif", fps=2)
    with Image.open(path) as gif:
        assert gif.n_frames == 5
        assert gif.size == (64, 48)


def test_gif_normalises_mismatched_frame_sizes(tmp_path: Path) -> None:
    """Frames of differing sizes are conformed to the first frame."""
    frames = make_frames(3)
    frames[1] = frames[1].resize((32, 24))
    path = write_gif(frames, tmp_path / "out.gif")
    with Image.open(path) as gif:
        assert gif.size == (64, 48)
        assert gif.n_frames == 3


def test_empty_animation_is_rejected(tmp_path: Path) -> None:
    """Building an animation from no frames is an explicit error."""
    with pytest.raises(ExportError, match="zero frames"):
        write_gif([], tmp_path / "out.gif")


@requires_ffmpeg
def test_mp4_frame_count_and_size(tmp_path: Path) -> None:
    """The encoded video has one frame per input at the requested size."""
    import imageio.v3 as iio

    path = write_mp4(make_frames(6, (128, 96)), tmp_path / "out.mp4", fps=3)
    video = iio.imread(path, plugin="FFMPEG")

    assert video.shape[0] == 6
    assert (video.shape[2], video.shape[1]) == (128, 96)


@requires_ffmpeg
@pytest.mark.parametrize(
    ("requested", "expected"),
    [((513, 385), (512, 384)), ((255, 255), (254, 254)), ((100, 100), (100, 100))],
)
def test_mp4_crops_to_even_without_rescaling(
    tmp_path: Path, requested: tuple[int, int], expected: tuple[int, int]
) -> None:
    """Odd dimensions are cropped to even, never rescaled to a macro-block grid.

    imageio's ffmpeg plugin defaults to resizing video to a multiple of 16,
    which would silently turn a 100x100 request into 96x96 and shift every
    pixel's geographic position. The encoder is configured to disable that.
    """
    import imageio.v3 as iio

    path = write_mp4(make_frames(3, requested), tmp_path / "out.mp4", fps=4)
    video = iio.imread(path, plugin="FFMPEG")

    assert (video.shape[2], video.shape[1]) == expected


@requires_ffmpeg
def test_mp4_frames_stay_distinct(tmp_path: Path) -> None:
    """Each source frame survives encoding as a visibly different video frame."""
    import imageio.v3 as iio
    import numpy as np

    path = write_mp4(make_frames(4, (64, 64)), tmp_path / "out.mp4", fps=2)
    video = iio.imread(path, plugin="FFMPEG")

    differences = [
        np.abs(video[index].astype(int) - video[index + 1].astype(int)).mean()
        for index in range(len(video) - 1)
    ]
    assert all(difference > 1.0 for difference in differences)


def test_mp4_without_imageio_explains_the_fix(tmp_path: Path, monkeypatch) -> None:
    """A missing backend produces an install hint rather than an ImportError."""
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *args, **kwargs):
        if name.startswith("imageio"):
            raise ImportError("no imageio")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(ExportError, match="imageio\\[ffmpeg\\]"):
        write_mp4(make_frames(2), tmp_path / "out.mp4")
