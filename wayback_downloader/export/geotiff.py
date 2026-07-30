"""Georeferenced raster output.

``rasterio`` produces a proper GeoTIFF with an embedded CRS and transform. When
it is not installed -- common, since it needs GDAL -- the module falls back to
a plain TIFF beside an ESRI world file and ``.prj`` sidecar, which every GIS
package reads as georeferenced data.

Both paths write EPSG:3857 (Web Mercator), the native CRS of the tiles, so no
resampling is involved.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from wayback_downloader.exceptions import ExportError
from wayback_downloader.gis.projection import lonlat_to_meters
from wayback_downloader.models import BoundingBox
from wayback_downloader.utils.logger import get_logger

logger = get_logger(__name__)

# OGC WKT for EPSG:3857, written next to the TIFF in the fallback path.
WEB_MERCATOR_WKT = (
    'PROJCS["WGS 84 / Pseudo-Mercator",GEOGCS["WGS 84",DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],'
    'UNIT["degree",0.0174532925199433]],PROJECTION["Mercator_1SP"],'
    'PARAMETER["central_meridian",0],PARAMETER["scale_factor",1],'
    'PARAMETER["false_easting",0],PARAMETER["false_northing",0],'
    'UNIT["metre",1],AXIS["X",EAST],AXIS["Y",NORTH],AUTHORITY["EPSG","3857"]]'
)


def _mercator_transform(
    bounds: BoundingBox, width: int, height: int
) -> tuple[float, float, float, float]:
    """Return ``(left, top, x_resolution, y_resolution)`` in EPSG:3857 metres."""
    left, top = lonlat_to_meters(bounds.west, bounds.north)
    right, bottom = lonlat_to_meters(bounds.east, bounds.south)
    return left, top, (right - left) / width, (top - bottom) / height


def write_geotiff(image: Image.Image, bounds: BoundingBox, path: Path) -> Path:
    """Write a georeferenced raster, using rasterio when it is available."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rgb = image.convert("RGB")
    left, top, x_res, y_res = _mercator_transform(bounds, rgb.width, rgb.height)

    try:
        import numpy as np
        import rasterio
        from rasterio.transform import Affine
    except ImportError:
        logger.debug("rasterio unavailable; writing TIFF with world-file sidecars")
        return _write_worldfile_tiff(rgb, path, left, top, x_res, y_res)

    transform = Affine(x_res, 0.0, left, 0.0, -y_res, top)
    array = np.asarray(rgb).transpose(2, 0, 1)

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=rgb.height,
        width=rgb.width,
        count=3,
        dtype=array.dtype,
        crs="EPSG:3857",
        transform=transform,
        compress="deflate",
        tiled=True,
    ) as dataset:
        dataset.write(array)

    return path


def _write_worldfile_tiff(
    image: Image.Image, path: Path, left: float, top: float, x_res: float, y_res: float
) -> Path:
    """Write a plain TIFF plus ``.tfw`` and ``.prj`` georeferencing sidecars."""
    try:
        image.save(path, format="TIFF", compression="tiff_deflate")
    except OSError as exc:
        raise ExportError(f"Could not write TIFF to {path}: {exc}") from exc

    # World-file coordinates refer to the centre of the top-left pixel.
    path.with_suffix(".tfw").write_text(
        "\n".join(
            (
                f"{x_res:.12f}",
                "0.0",
                "0.0",
                f"{-y_res:.12f}",
                f"{left + x_res / 2:.6f}",
                f"{top - y_res / 2:.6f}",
            )
        )
        + "\n",
        encoding="ascii",
    )
    path.with_suffix(".prj").write_text(WEB_MERCATOR_WKT, encoding="ascii")
    return path
