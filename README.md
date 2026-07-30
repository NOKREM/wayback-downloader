# Wayback Downloader

A production-ready CLI for downloading historical satellite imagery from the
[Esri Living Atlas World Imagery Wayback](https://livingatlas.arcgis.com/wayback/)
archive — every version of the World Imagery basemap published since 2014.

Give it a coordinate, a date, a zoom level and an image size. It finds the
Wayback release closest to that date *at that specific location*, downloads the
tiles concurrently, stitches them into one image centred exactly on your
coordinate, and writes a JSON sidecar describing where the imagery came from.

No GUI. No hard-coded service URLs.

---

## 1. Endpoint architecture

The Wayback system is three REST surfaces. This project discovers all of them
at runtime from a single bootstrap document.

### 1.1 Bootstrap configuration

```
GET https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json
```

A JSON object keyed by *release number*, currently 195 entries spanning
2014-02-20 to 2026-06-30:

```json
{
  "32246": {
    "itemID": "ad62e5488ac441cbaf487ac9268c0590",
    "itemTitle": "World Imagery (Wayback 2026-06-30)",
    "itemURL": ".../MapServer/tile/32246/{level}/{row}/{col}",
    "metadataLayerUrl": ".../World_Imagery_Metadata_2026_r06/MapServer",
    "metadataLayerItemID": "91d6bfe372d043e69d99b3f478872e96",
    "layerIdentifier": "WB_2026_R06"
  }
}
```

> **The release number is not a clock.** Release `64776` is from 2023 while
> `64001` is from 2026. Sorting by release number produces a scrambled
> timeline. The authoritative date lives in `itemTitle` and is parsed out of it.
> Everything in this project orders releases by parsed date.

### 1.2 Tile service (WMTS)

```
GET {base}/tile/{releaseNum}/{level}/{row}/{col}      -> image/jpeg
```

Standard XYZ tiles, 256x256, Web Mercator (EPSG:3857). `{base}` is **derived**
from the `itemURL` templates rather than hard-coded — see §3.

### 1.3 Tilemap resource — the key to local change detection

```
GET {base}/tilemap/{releaseNum}/{level}/{row}/{col}/1/1
```

```json
{"data":[1],"location":{"height":1,"left":9419,"top":6273,"width":1},
 "size":[26202],"valid":true}
```

This reports whether a tile exists (`valid`) and **how many bytes it occupies**
(`size`) without transferring the image.

All 195 releases serve a tile at any given location, but most are byte-identical
reissues of the previous one. Walking the releases in chronological order and
keeping only those whose byte size changed yields exactly the dates on which the
imagery at that spot was actually updated. In practice this reduces 195 releases
to a handful — at the coordinate `38.7992, 26.9723` and zoom 18 it is 5 — at a
cost of one small JSON response each instead of 195 image downloads.

### 1.4 Metadata service

```
GET {metadataLayerUrl}/identify?f=json&geometry={...}&layers=all&...
```

Returns the source-imagery footprint covering the point:

| Attribute | Meaning |
|---|---|
| `SRC_DATE` / `SRC_DATE2` | Acquisition date of the source imagery (`YYYYMMDD`) |
| `SRC_RES` | Native sensor resolution, metres |
| `SAMP_RES` | Resampled resolution as served, metres |
| `SRC_DESC` | Sensor (`WV03`, `LG02`, …) |
| `NICE_DESC` | Provider (`Maxar`, `Vantor`, …) |
| `NICE_NAME` | Product (`Vivid`, `Metro`, …) |
| `MinMapLevel` / `MaxMapLevel` | Zoom range this record renders at |

The service splits this across one feature layer per resolution band (1.9 cm
through 2.4 m) and `identify` returns several overlapping records. The one that
actually renders at your zoom is the record whose `MinMapLevel..MaxMapLevel`
range contains it — taking the first result would often report the wrong band.

---

## 2. Request flow

```
  wayback download --lat 38.7992 --lon 26.9723 --date 2022-04-15 --zoom 18 --size 1024
        |
   [1]  validate  ------------------ coordinate, date, zoom, size
        |
   [2]  GET waybackconfig.json  ---- 195 releases, cached 6h
        |                            derive tile service base from itemURL
        |
   [3]  lon/lat -> XYZ tile  ------- 18/150712/100373
        |
   [4]  GET tilemap/{rel}/18/...  -- x195, concurrent
        |                            dedupe by byte size
        |                            -> 5 releases with local changes
        |
   [5]  nearest date to 2022-04-15 - 2023-03-15 (release 44873)
        |
   [6]  plan tile grid  ------------ 5x5 = 25 tiles, crop box for 1024x1024
        |
   [7]  GET tile/44873/18/...  ----- x25, concurrent, retried, cached 7d
        |
   [8]  GET {metadata}/identify  --- Maxar / Vivid / WV03 / 2022-10-05 / 0.31 m
        |
   [9]  stitch 1280x1280 mosaic, crop to 1024x1024 centred on the coordinate
        |
  [10]  write output/2023-03-15.png + output/2023-03-15.json
```

Steps 4 and 7 are the only expensive ones and both run concurrently under a
shared connection pool, semaphore and rate limiter.

---

## 3. How endpoint changes are absorbed

The project has exactly **one** network constant: the bootstrap URL (plus two
fallback mirrors). Everything else adapts.

* **Service base is derived, not declared.** The tile service base is extracted
  from the `itemURL` templates the service publishes for itself, by matching
  `…/tile/{releaseNum}/{level}/{row}/{col}` and taking the **majority vote**
  across all releases — so one malformed entry cannot redirect every request to
  the wrong host. If Esri moves the service or renames the WMTS path, the new
  base is picked up on the next run.
* **Field names are aliased.** Every config field has a list of accepted
  spellings (`itemID` / `itemId` / `item_id` / `id`, …). A renamed key does not
  break the run.
* **Placeholders are normalised.** `{z}/{y}/{x}` templates are rewritten to the
  canonical `{level}/{row}/{col}` form.
* **Document shape is flexible.** Both an object keyed by release number and a
  plain array of records are handled.
* **Malformed records are dropped, not fatal.** One unparseable entry never
  aborts the catalog load.
* **Drift is reported.** `detect_schema_drift` warns when fields disappear or
  records stop parsing, so a contract change surfaces as a visible warning
  instead of a silent behaviour change.
* **Requests look like the web app's.** Browser `User-Agent`, `Origin`,
  `Referer`, `Accept` and `Sec-Fetch-*` headers, over a pooled keep-alive
  session with HTTP/2 where available.

Run `wayback endpoints` to print everything discovered at runtime.

---

## 4. Install

Requires Python 3.12+.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Optional extras — the core downloader works without any of them:

```bash
pip install -r requirements-optional.txt
```

| Package | Enables |
|---|---|
| `rasterio` | True GeoTIFF output (otherwise TIFF + `.tfw` + `.prj` world files) |
| `imageio[ffmpeg]` | MP4 time-lapse (`--mp4`) |
| `mercantile`, `pyproj` | Cross-checks the in-house tile/projection maths in tests |
| `shapely` | Extra geometry operations |

Installing `mercantile` and `pyproj` un-skips two tests that validate the
in-house tile addressing and Web Mercator transforms against those reference
implementations. `imageio-ffmpeg` bundles its own ffmpeg binary, so no system
ffmpeg install is needed.

`--mp4` encodes H.264 (High profile, `yuv420p`) at the requested `--fps`.
Because `yuv420p` subsamples chroma, odd frame dimensions are **cropped** by one
pixel to the nearest even size — never rescaled, so a pixel's geographic
position is preserved. A 513x385 request yields a 512x384 video.

---

### Android / Termux

The code itself is platform-neutral — pure-Python tile and projection maths, no
`os.name`/`sys.platform` branches, no shell-outs, all paths via `pathlib`. What
takes work on Termux is the dependency chain, because Android uses bionic libc
and PyPI's `manylinux` wheels are glibc-only, so anything with a compiled
extension must come from Termux's own repository or be built locally.

```bash
pkg install python python-pillow rust
pip install httpx typer rich diskcache tqdm
pip install pydantic pydantic-settings   # builds pydantic-core; ~10-15 min
python main.py download --lat 38.7992 --lon 26.9723 --date 2022-04-15 --zoom 17
```

| Dependency | On Termux |
|---|---|
| `httpx`, `typer`, `rich`, `diskcache`, `tqdm`, `h2` | Pure Python — install normally |
| `pillow` | `pkg install python-pillow` (avoids building against libjpeg/libpng) |
| `pydantic-core` | Rust extension, no Android wheels on PyPI. Either `pkg install rust` and let pip build it, or use a prebuilt Android wheel index. The long pole. |
| `rasterio`, `pyproj` | Needs GDAL/PROJ; skip them. GeoTIFF still works through the world-file fallback (§6) |
| `imageio-ffmpeg` | Ships no Android binary. Use Termux's own: `pkg install ffmpeg` and `export IMAGEIO_FFMPEG_EXE=$(command -v ffmpeg)` |

Worth setting on a phone:

```bash
export WAYBACK_MAX_CONCURRENCY=6        # gentler on a mobile connection
export WAYBACK_CACHE_DIR=~/wayback-cache
termux-setup-storage                    # only if you want output on shared storage
```

> Not verified on a device — this reflects the dependency situation and the
> code's platform assumptions, not a test run.

## 5. Usage

### Download one image

```bash
python main.py download --lat 38.7992 --lon 26.9723 --date 2022-04-15 --zoom 18 --size 1024
```

```
Saved output/2023-03-15.png
  Requested date         2022-04-15
  Matched release        2023-03-15 (+334 days)
  Release number         44873
  Imagery provider       Maxar
  Acquisition date       2022-10-05
  Resolution             46.5 cm/px
  Tiles                  25
```

### Working across zoom levels — `--zoom-range`

`--zoom-range` is available on **every location-based command** (`download`,
`versions`, `range`, `all`, `timelapse`, `bbox`, `batch`). It replaces `--zoom`
and accepts a span, a list, or `all`:

```bash
python main.py download --lat 38.7992 --lon 26.9723 --date 2022-04-15 --zoom-range 15-18
python main.py download --lat 38.7992 --lon 26.9723 --date 2022-04-15 --zoom-range 12,15,18
python main.py download --lat 38.7992 --lon 26.9723 --date 2022-04-15 --zoom-range all
```

`all` is not a guess: it reads the `MinMapLevel`/`MaxMapLevel` of every imagery
footprint covering the point (§1.4) and uses exactly the levels the service
publishes there — zoom 3–19 at the example coordinate.

```
┌──────┬──────────────┬────────┬────────────┬───────┬────────────────────┐
│ Zoom │ Matched date │ Offset │ Resolution │ Tiles │ File               │
├──────┼──────────────┼────────┼────────────┼───────┼────────────────────┤
│   15 │ 2022-04-06   │    -9d │  3.72 m/px │     9 │ 2022-04-06_z15.png │
│   16 │ 2023-03-15   │  +334d │  1.86 m/px │     9 │ 2023-03-15_z16.png │
│   17 │ 2023-03-15   │  +334d │ 93.1 cm/px │     9 │ 2023-03-15_z17.png │
│   18 │ 2023-03-15   │  +334d │ 46.5 cm/px │     9 │ 2023-03-15_z18.png │
└──────┴──────────────┴────────┴────────────┴───────┴────────────────────┘
```

> **Each level resolves its own releases, and they genuinely differ.** Local
> change detection is per-tile, and a zoom-15 tile covers 16x more ground than a
> zoom-17 one, so it picks up updates the higher levels never see. Above, zoom
> 15 lands within 9 days of the request while zooms 16–18 are 334 days off —
> same coordinate, same requested date. Levels are therefore resolved
> independently, never once and reused. Filenames carry a `_z{level}` suffix so
> two levels landing on the same date cannot overwrite each other.

`versions` makes that structure visible directly — a date-by-zoom matrix instead
of one list per level:

```bash
python main.py versions --lat 38.7992 --lon 26.9723 --zoom-range 14-19
```

```
┌────────────┬────┬────┬────┬────┬────┬────┐
│ Date       │ 14 │ 15 │ 16 │ 17 │ 18 │ 19 │
├────────────┼────┼────┼────┼────┼────┼────┤
│ 2014-11-12 │ *  │ -  │ -  │ -  │ -  │ -  │
│ 2020-03-23 │ -  │ -  │ *  │ *  │ *  │ *  │
│ 2022-04-06 │ *  │ *  │ -  │ -  │ -  │ -  │
│ 2023-03-15 │ *  │ *  │ *  │ *  │ *  │ -  │
│ 2026-02-26 │ *  │ *  │ -  │ -  │ -  │ -  │
└────────────┴────┴────┴────┴────┴────┴────┘
```

Per-command behaviour:

| Command | With `--zoom-range` |
|---|---|
| `download` | One image per level, nearest release resolved per level |
| `versions` | Date-by-zoom availability matrix |
| `range` | Every release in the window, per level |
| `all` | Every changed release, per level |
| `timelapse` | One animation **per level** (`..._z16.gif`); levels are never mixed into one animation, since frames at different zooms show different ground extents |
| `bbox` | The box at each level — output area grows 4x per level, so a wide span can hit the tile cap at the top end |
| `batch` | Applies to every row, overriding any per-row `zoom` column |

A level with no imagery, or too few frames to animate, is reported and skipped
rather than failing the run.

### List what exists at a location

```bash
python main.py versions --lat 38.7992 --lon 26.9723 --zoom 18
```

### Every version, a date range, a time-lapse

```bash
python main.py all --lat 38.7992 --lon 26.9723 --zoom 17 --size 512 --gif
python main.py range --lat 38.7992 --lon 26.9723 --start 2020-01-01 --end 2023-12-31 --zoom 17
python main.py timelapse --lat 38.7992 --lon 26.9723 --zoom 17 --size 512 --mp4
```

### Bounding box, with georeferenced output

```bash
python main.py bbox --west 26.965 --south 38.795 --east 26.980 --north 38.805 \
  --date 2023-06-01 --zoom 16 --geotiff
```

### Many coordinates from CSV or GeoJSON

```bash
python main.py batch points.csv --date 2022-04-15 --zoom 17
python main.py batch points.geojson --zoom 18
```

CSV columns are matched case-insensitively and accept aliases including
`lat`/`latitude`/`y`/`enlem` and `lon`/`lng`/`longitude`/`x`/`boylam`.
Per-row `date` and `zoom` columns override the command-line defaults.

### Cache and diagnostics

```bash
python main.py endpoints          # show discovered REST endpoints
python main.py cache              # cache location and size
python main.py cache --clear
python main.py --no-cache download ...
python main.py --verbose download ...
```

---

## 6. Output

```
output/
├── 2023-03-15.png     # image, centred exactly on the requested coordinate
├── 2023-03-15.json    # metadata sidecar
├── 2023-03-15.tif     # with --geotiff
├── 2023-03-15.tfw     #   world file — fallback only, when rasterio is absent
└── 2023-03-15.prj     #   EPSG:3857 WKT — fallback only
```

With `rasterio` installed, `--geotiff` writes a single self-describing GeoTIFF
(EPSG:3857 embedded, 3x uint8 bands, deflate-compressed, internally tiled
256x256) and no sidecars.

> **Two different resolution numbers.** The GeoTIFF transform reports metres per
> pixel in EPSG:3857 (2.39 at zoom 16 near latitude 38.8), while
> `ground_resolution_m_per_px` in the JSON reports true ground metres (1.86).
> Web Mercator stretches distance by `1 / cos(latitude)`; both numbers are
> correct and differ by exactly that factor.

```json
{
  "latitude": 38.7992,
  "longitude": 26.9723,
  "requested_date": "2022-04-15",
  "matched_date": "2023-03-15",
  "date_offset_days": 334,
  "zoom": 18,
  "layer_id": "ee531a31beda4529ad66edcbe9fde701",
  "release_num": 44873,
  "layer_identifier": "WB_2023_R02",
  "service_url": ".../World_Imagery/WMTS/1.0.0/default028mm/MapServer",
  "tile_url_template": ".../tile/44873/{level}/{row}/{col}",
  "metadata_service_url": ".../World_Imagery_Metadata_2023_r02/MapServer",
  "imagery_provider": "Maxar",
  "imagery_product": "Vivid",
  "imagery_sensor": "WV03",
  "imagery_acquisition_date": "2022-10-05",
  "resolution": "46.5 cm/px",
  "ground_resolution_m_per_px": 0.465398,
  "source_resolution_m": 0.31,
  "tile_count": 25,
  "tile_grid": { "z": 18, "min_x": 150710, "min_y": 100371, "columns": 5, "rows": 5 },
  "bounds_wgs84": { "west": 26.96955264, "south": 38.797063,
                    "east": 26.9750458, "north": 38.80134407 },
  "image_size": { "width": 1024, "height": 1024 }
}
```

---

## 7. Architecture

```
wayback_downloader/
├── cli.py            Typer commands, argument validation, exit codes
├── config.py         Settings (env-overridable, WAYBACK_ prefix)
├── models.py         Pydantic domain models
├── exceptions.py     Error hierarchy, one exit code per failure mode
├── service.py        Orchestration — the API a library consumer uses
│
├── api/
│   ├── discovery.py  Runtime endpoint discovery + schema-drift detection
│   ├── wayback.py    Release catalog, local change detection, date matching
│   ├── imagery.py    Release + viewport -> finished image
│   ├── metadata.py   identify lookups, resolution-band selection
│   └── downloader.py Concurrent tile retrieval with retry and caching
│
├── gis/
│   ├── projection.py Web Mercator maths (pure Python, no GDAL)
│   ├── tiles.py      XYZ addressing, grid planning, crop-window computation
│   └── stitch.py     Pillow mosaic, crop, PNG/JPEG encoding
│
├── export/
│   ├── geotiff.py    GeoTIFF via rasterio, world-file fallback
│   └── animation.py  GIF via Pillow, MP4 via imageio
│
├── utils/
│   ├── http.py       Pooled async client, browser-shaped headers
│   ├── retry.py      Exponential backoff, jitter, Retry-After handling
│   ├── cache.py      diskcache with in-memory fallback
│   ├── progress.py   Progress reporting behind an interface
│   ├── validator.py  Input validation with actionable messages
│   ├── inputs.py     CSV / GeoJSON batch parsing
│   └── logger.py     Rich logging
│
└── tests/            89 unit tests
```

### Use as a library

```python
import asyncio, datetime as dt
from wayback_downloader.models import Coordinate, DownloadRequest
from wayback_downloader.service import WaybackService

async def main():
    async with WaybackService() as service:
        request = DownloadRequest(
            coordinate=Coordinate(latitude=38.7992, longitude=26.9723),
            requested_date=dt.date(2022, 4, 15),
            zoom=18, width=1024, height=1024,
        )
        result = await service.download(request)
        print(result.image_path, result.metadata.imagery_provider)

asyncio.run(main())
```

---

## 8. Reliability

| Concern | Handling |
|---|---|
| Invalid coordinate / date / zoom / size | Validated up front with an actionable message; exit code 2 |
| Config endpoint unreachable | Two fallback mirrors, then an explicit discovery error; exit 3 |
| Endpoint schema changed | Field aliasing, placeholder normalisation, drift warnings; exit 3 |
| No imagery at the location | Suggests a lower zoom; exit 4 |
| HTTP 5xx / timeout / network error | Exponential backoff with jitter, 4 retries |
| HTTP 429 / 503 rate limit | Honours `Retry-After` (both seconds and HTTP-date); exit 6 |
| A few missing tiles | Left transparent, warned about |
| Many missing tiles (>15%) | Rejected rather than returning a misleading image; exit 5 |
| Corrupt tile bytes | Skipped without aborting the mosaic |
| Wrong-sized tile | Resampled to fit the grid |
| Antimeridian crossing | Tile columns wrap; rows outside the pyramid are dropped |
| Oversized request | Refused above 4096 tiles with a size/zoom hint |

### Performance

* Async tile and tilemap downloads over one pooled HTTP/2 connection set
* Concurrency capped by semaphore (`WAYBACK_MAX_CONCURRENCY`, default 16)
* Optional pacing floor (`WAYBACK_MIN_REQUEST_INTERVAL`) for extra politeness
* Persistent disk cache: tiles 7 days, catalog 6 hours, metadata 1 day
* Progress bars for both the version probe and the tile download

---

## 9. Development

```bash
pip install -r requirements-dev.txt
pytest -q
black wayback_downloader main.py
mypy wayback_downloader
```

The test suite runs entirely offline. Where `mercantile` or `pyproj` are
installed, the in-house tile and projection maths are cross-checked against
them; otherwise those tests skip.

---

## 10. Attribution

Imagery is served by Esri and its partners (Maxar, Vantor, and others named in
each image's metadata sidecar). Review the
[World Imagery Wayback item page](https://www.arcgis.com/home/item.html?id=8d47b1f2ccf141bbab8b73f5f8acc979)
and Esri's terms of use before redistributing anything you download.
