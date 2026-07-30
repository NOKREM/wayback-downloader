"""Pydantic domain models shared across every layer of the application."""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Latitude = Annotated[float, Field(ge=-90.0, le=90.0)]
Longitude = Annotated[float, Field(ge=-180.0, le=180.0)]
ZoomLevel = Annotated[int, Field(ge=0, le=23)]

_TITLE_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


class Coordinate(BaseModel):
    """A WGS84 geographic point."""

    model_config = ConfigDict(frozen=True)

    latitude: Latitude
    longitude: Longitude

    @model_validator(mode="after")
    def _reject_mercator_poles(self) -> "Coordinate":
        """Reject latitudes outside the Web Mercator valid band.

        Web Mercator is undefined at the poles; the tile pyramid is clipped at
        +/-85.0511 degrees, so a point beyond that has no tile to download.
        """
        if abs(self.latitude) > 85.05112878:
            raise ValueError(
                f"latitude {self.latitude} is outside the Web Mercator range "
                "(-85.05112878 .. 85.05112878)"
            )
        return self

    def __str__(self) -> str:
        """Render as a compact 'lat,lon' pair."""
        return f"{self.latitude:.6f},{self.longitude:.6f}"


class BoundingBox(BaseModel):
    """A WGS84 bounding box in (west, south, east, north) order."""

    model_config = ConfigDict(frozen=True)

    west: Longitude
    south: Latitude
    east: Longitude
    north: Latitude

    @model_validator(mode="after")
    def _check_ordering(self) -> "BoundingBox":
        """Ensure the box is non-degenerate and correctly ordered."""
        if self.west >= self.east:
            raise ValueError("bounding box west must be smaller than east")
        if self.south >= self.north:
            raise ValueError("bounding box south must be smaller than north")
        return self

    @property
    def center(self) -> Coordinate:
        """Return the geographic centre of the box."""
        return Coordinate(
            latitude=(self.south + self.north) / 2.0,
            longitude=(self.west + self.east) / 2.0,
        )


class TileIndex(BaseModel):
    """A single XYZ tile address."""

    model_config = ConfigDict(frozen=True)

    z: ZoomLevel
    x: int = Field(ge=0)
    y: int = Field(ge=0)

    def __str__(self) -> str:
        """Render as 'z/x/y'."""
        return f"{self.z}/{self.x}/{self.y}"


class TilePlacement(BaseModel):
    """A fetchable tile together with where it belongs in the mosaic.

    The paste offset is kept separate from the tile address because a grid that
    crosses the antimeridian wraps its column indices, making two distinct
    mosaic positions resolve to the same tile.
    """

    model_config = ConfigDict(frozen=True)

    index: TileIndex
    offset_x: int
    offset_y: int


class TileGrid(BaseModel):
    """A rectangular block of tiles plus the pixel window to crop from it.

    ``crop_box`` is expressed in pixels relative to the top-left corner of the
    stitched mosaic, so the caller never has to redo the mercator maths.
    ``min_x``/``max_x`` may fall outside the valid tile range when the grid
    crosses the antimeridian; :meth:`placements` normalises them.
    """

    model_config = ConfigDict(frozen=True)

    z: ZoomLevel
    min_x: int
    min_y: int
    max_x: int
    max_y: int
    tile_size: int = 256
    crop_box: tuple[int, int, int, int]

    @property
    def columns(self) -> int:
        """Number of tile columns in the grid."""
        return self.max_x - self.min_x + 1

    @property
    def rows(self) -> int:
        """Number of tile rows in the grid."""
        return self.max_y - self.min_y + 1

    @property
    def mosaic_size(self) -> tuple[int, int]:
        """Pixel dimensions of the fully stitched mosaic."""
        return self.columns * self.tile_size, self.rows * self.tile_size

    def placements(self) -> list[TilePlacement]:
        """Enumerate every fetchable tile with its mosaic paste offset.

        Columns wrap around the antimeridian; rows that fall outside the
        pyramid are dropped, leaving those areas blank in the mosaic.
        """
        span = 1 << self.z
        result: list[TilePlacement] = []
        for row, y in enumerate(range(self.min_y, self.max_y + 1)):
            if not 0 <= y < span:
                continue
            for column, x in enumerate(range(self.min_x, self.max_x + 1)):
                result.append(
                    TilePlacement(
                        index=TileIndex(z=self.z, x=x % span, y=y),
                        offset_x=column * self.tile_size,
                        offset_y=row * self.tile_size,
                    )
                )
        return result

    @property
    def tile_count(self) -> int:
        """Number of tiles that actually need to be fetched."""
        return len(self.placements())


class WaybackRelease(BaseModel):
    """One published version of the World Imagery basemap.

    The release number is an opaque service identifier, *not* a chronological
    counter -- release 64776 is from 2023 while 64001 is from 2026. The
    authoritative date lives in the item title and is parsed out here.
    """

    model_config = ConfigDict(frozen=True)

    release_num: int
    item_id: str
    title: str
    tile_url_template: str
    metadata_url: str | None = None
    metadata_item_id: str | None = None
    layer_identifier: str | None = None
    release_date: dt.date

    @classmethod
    def parse_release_date(cls, title: str) -> dt.date:
        """Extract the release date embedded in a Wayback item title.

        Titles follow the pattern ``World Imagery (Wayback YYYY-MM-DD)``.
        """
        match = _TITLE_DATE_RE.search(title)
        if match is None:
            raise ValueError(f"no release date found in title {title!r}")
        year, month, day = (int(part) for part in match.groups())
        return dt.date(year, month, day)

    def tile_url(self, tile: TileIndex) -> str:
        """Build the fully-qualified tile URL for a tile address."""
        return (
            self.tile_url_template.replace("{level}", str(tile.z))
            .replace("{row}", str(tile.y))
            .replace("{col}", str(tile.x))
        )

    @property
    def item_page_url(self) -> str:
        """Public ArcGIS Online item page for this release."""
        return f"https://www.arcgis.com/home/item.html?id={self.item_id}"


class ServiceEndpoints(BaseModel):
    """Endpoints discovered at runtime from the remote Wayback configuration.

    Nothing here is hard-coded into the request path: ``tile_service_base`` is
    derived from the tile URL template published by the service itself, so an
    Esri-side host or path change is picked up automatically.
    """

    model_config = ConfigDict(frozen=True)

    config_url: str
    tile_service_base: str
    release_count: int
    discovered_at: dt.datetime

    def tilemap_url(
        self, release_num: int, tile: TileIndex, width: int = 1, height: int = 1
    ) -> str:
        """Build a tilemap probe URL.

        The tilemap resource reports whether a tile exists and how many bytes it
        occupies without transferring the image, which is what makes local
        change detection cheap.
        """
        return (
            f"{self.tile_service_base}/tilemap/{release_num}"
            f"/{tile.z}/{tile.y}/{tile.x}/{width}/{height}"
        )


class TilemapProbe(BaseModel):
    """Result of a tilemap request for one release at one tile address."""

    model_config = ConfigDict(frozen=True)

    release_num: int
    valid: bool
    byte_size: int | None

    @property
    def has_imagery(self) -> bool:
        """True when the release actually serves a non-empty tile here."""
        return self.valid and bool(self.byte_size)


class ImageryMetadata(BaseModel):
    """Source-imagery attributes reported by the Wayback metadata service."""

    model_config = ConfigDict(frozen=True)

    provider: str | None = None
    product: str | None = None
    sensor: str | None = None
    acquisition_date: dt.date | None = None
    source_resolution_m: float | None = None
    sampled_resolution_m: float | None = None
    accuracy_m: float | None = None
    min_zoom: int | None = None
    max_zoom: int | None = None


class DownloadRequest(BaseModel):
    """A fully validated user request for one image."""

    model_config = ConfigDict(frozen=True)

    coordinate: Coordinate
    requested_date: dt.date
    zoom: ZoomLevel
    width: int = Field(gt=0, le=16384)
    height: int = Field(gt=0, le=16384)
    image_format: Literal["png", "jpg"] = "png"
    only_local_changes: bool = True

    @property
    def size(self) -> tuple[int, int]:
        """Requested output size as a ``(width, height)`` pair."""
        return self.width, self.height


class OutputMetadata(BaseModel):
    """The JSON sidecar written next to every produced image."""

    latitude: float
    longitude: float
    requested_date: dt.date
    matched_date: dt.date
    date_offset_days: int
    zoom: int
    layer_id: str
    release_num: int
    layer_identifier: str | None
    service_url: str
    tile_url_template: str
    metadata_service_url: str | None
    imagery_provider: str | None
    imagery_product: str | None
    imagery_sensor: str | None
    imagery_acquisition_date: dt.date | None
    resolution: str
    ground_resolution_m_per_px: float
    source_resolution_m: float | None
    tile_count: int
    tile_grid: dict[str, int]
    bounds_wgs84: dict[str, float]
    image_size: dict[str, int]
    image_file: str
    generated_at: dt.datetime
    tool_version: str
