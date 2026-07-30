"""Input validation that converts user mistakes into actionable messages."""

from __future__ import annotations

import datetime as dt
import re

from pydantic import ValidationError as PydanticValidationError

from wayback_downloader.exceptions import ValidationError
from wayback_downloader.models import BoundingBox, Coordinate

_SIZE_RE = re.compile(r"^\s*(\d+)\s*(?:[x×*]\s*(\d+))?\s*$", re.IGNORECASE)

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%Y%m%d")

MIN_WAYBACK_DATE = dt.date(2014, 1, 1)


def validate_coordinate(latitude: float, longitude: float) -> Coordinate:
    """Validate a latitude/longitude pair.

    Rejects out-of-range values and points beyond the Web Mercator cutoff,
    where no tile pyramid exists.
    """
    try:
        return Coordinate(latitude=latitude, longitude=longitude)
    except PydanticValidationError as exc:
        detail = "; ".join(error["msg"] for error in exc.errors())
        raise ValidationError(f"Invalid coordinate ({latitude}, {longitude}): {detail}") from exc


def validate_date(value: str | dt.date) -> dt.date:
    """Parse and sanity-check a requested date.

    Accepts ``YYYY-MM-DD`` plus a few common alternatives. Dates before the
    Wayback archive begins, or in the future, are rejected outright because no
    release can ever match them.
    """
    if isinstance(value, dt.datetime):
        parsed = value.date()
    elif isinstance(value, dt.date):
        parsed = value
    else:
        text = value.strip()
        parsed = None  # type: ignore[assignment]
        for fmt in _DATE_FORMATS:
            try:
                parsed = dt.datetime.strptime(text, fmt).date()
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValidationError(
                f"Invalid date {value!r}. Expected YYYY-MM-DD (for example 2021-05-14)."
            )

    today = dt.date.today()
    if parsed < MIN_WAYBACK_DATE:
        raise ValidationError(
            f"Date {parsed.isoformat()} predates the Wayback archive, "
            f"which starts at {MIN_WAYBACK_DATE.isoformat()}."
        )
    if parsed > today:
        raise ValidationError(f"Date {parsed.isoformat()} is in the future.")
    return parsed


def validate_zoom(zoom: int) -> int:
    """Validate a tile zoom level against the World Imagery pyramid depth."""
    if not isinstance(zoom, int) or isinstance(zoom, bool):
        raise ValidationError(f"Invalid zoom {zoom!r}: must be an integer.")
    if not 0 <= zoom <= 23:
        raise ValidationError(f"Invalid zoom {zoom}: must be between 0 and 23.")
    return zoom


def parse_zoom_levels(value: str) -> list[int] | None:
    """Parse a ``--zoom-range`` argument into an explicit list of zoom levels.

    Accepts three forms:

    * ``"14-19"`` -- an inclusive span,
    * ``"12,15,18"`` -- an explicit list (spans may be mixed in, e.g. ``"10,14-16"``),
    * ``"all"`` -- returns ``None``, signalling the caller to discover the levels
      that actually carry imagery at the requested location.

    Levels are de-duplicated and returned in ascending order.
    """
    text = str(value).strip().lower()
    if not text:
        raise ValidationError(
            "Empty zoom range. Use a span like 14-19, a list like 12,15,18, or 'all'."
        )
    if text == "all":
        return None

    levels: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low_text, _, high_text = part.partition("-")
            try:
                low, high = int(low_text.strip()), int(high_text.strip())
            except ValueError:
                raise ValidationError(
                    f"Invalid zoom span {part!r}. Use two integers, for example 14-19."
                ) from None
            if low > high:
                raise ValidationError(f"Invalid zoom span {part!r}: {low} is greater than {high}.")
            levels.update(range(validate_zoom(low), validate_zoom(high) + 1))
        else:
            try:
                levels.add(validate_zoom(int(part)))
            except ValueError:
                raise ValidationError(
                    f"Invalid zoom level {part!r}: expected an integer."
                ) from None

    if not levels:
        raise ValidationError(
            f"No zoom levels parsed from {value!r}. Use 14-19, 12,15,18, or 'all'."
        )
    return sorted(levels)


def parse_size(value: str | int) -> tuple[int, int]:
    """Parse a ``--size`` argument into a ``(width, height)`` pair.

    Accepts ``1024`` (square) and ``1024x768`` (explicit). The upper bound
    keeps a single request from expanding into tens of thousands of tiles.
    """
    if isinstance(value, int):
        width = height = value
    else:
        match = _SIZE_RE.match(str(value))
        if not match:
            raise ValidationError(
                f"Invalid size {value!r}. Use 1024 for a square image or 1024x768 for a rectangle."
            )
        width = int(match.group(1))
        height = int(match.group(2)) if match.group(2) else width

    if width <= 0 or height <= 0:
        raise ValidationError(f"Invalid size {value!r}: dimensions must be positive.")
    if width > 16384 or height > 16384:
        raise ValidationError(
            f"Invalid size {value!r}: each dimension must be 16384 pixels or fewer."
        )
    return width, height


def validate_bbox(west: float, south: float, east: float, north: float) -> BoundingBox:
    """Validate a WGS84 bounding box given in west/south/east/north order."""
    try:
        return BoundingBox(west=west, south=south, east=east, north=north)
    except PydanticValidationError as exc:
        detail = "; ".join(error["msg"] for error in exc.errors())
        raise ValidationError(
            f"Invalid bounding box ({west}, {south}, {east}, {north}): {detail}"
        ) from exc


def validate_date_range(start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
    """Ensure a start/end date pair is ordered correctly."""
    if start > end:
        raise ValidationError(
            f"Start date {start.isoformat()} is after end date {end.isoformat()}."
        )
    return start, end
