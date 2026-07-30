"""Runtime configuration.

Every value can be overridden with a ``WAYBACK_``-prefixed environment
variable, e.g. ``WAYBACK_MAX_CONCURRENCY=32``.

The only network constant here is the bootstrap configuration document. All
other endpoints are discovered from it at runtime -- see
:mod:`wayback_downloader.api.discovery`.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def default_cache_dir() -> Path:
    """Return the per-user cache directory for this tool.

    Deliberately *not* derived from the package location: when the package is
    pip-installed, that location is inside ``site-packages``, and a multi-gigabyte
    tile cache does not belong in an installation directory. Honours
    ``XDG_CACHE_HOME`` on Linux and Android/Termux, ``LOCALAPPDATA`` on Windows,
    and falls back to ``~/.cache``.
    """
    if xdg := os.environ.get("XDG_CACHE_HOME"):
        base = Path(xdg)
    elif sys.platform == "win32" and (local := os.environ.get("LOCALAPPDATA")):
        base = Path(local)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path.home() / ".cache"
    return base / "wayback-downloader"


BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Mirrors used when the primary bootstrap document is unreachable. Discovery
# walks this list in order, so a dead primary degrades to a slower start rather
# than a hard failure.
CONFIG_URL_CANDIDATES: tuple[str, ...] = (
    "https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json",
    "https://config.maptiles.arcgis.com/waybackconfig.json",
    "https://livingatlas.arcgis.com/wayback/waybackconfig.json",
)


class Settings(BaseSettings):
    """Application settings resolved from the environment."""

    model_config = SettingsConfigDict(
        env_prefix="WAYBACK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    config_url: str = CONFIG_URL_CANDIDATES[0]
    user_agent: str = BROWSER_USER_AGENT

    # Relative to the working directory, so running from the repository writes
    # to ./output as before while an installed `wayback` writes where it is run
    # rather than into site-packages.
    output_dir: Path = Path("output")
    cache_dir: Path = Field(default_factory=default_cache_dir)

    request_timeout: float = Field(default=30.0, gt=0)
    connect_timeout: float = Field(default=10.0, gt=0)

    max_concurrency: int = Field(default=16, ge=1, le=128)
    max_retries: int = Field(default=4, ge=0, le=10)
    retry_backoff: float = Field(default=0.5, gt=0)
    retry_backoff_max: float = Field(default=20.0, gt=0)

    # Minimum seconds between two request starts on a single host. Guards the
    # service against bursts even when concurrency is raised.
    min_request_interval: float = Field(default=0.0, ge=0)

    catalog_cache_ttl: int = Field(default=21_600, ge=0)  # 6 hours
    tile_cache_ttl: int = Field(default=604_800, ge=0)  # 7 days
    metadata_cache_ttl: int = Field(default=86_400, ge=0)  # 1 day
    cache_size_limit: int = Field(default=2 * 1024**3, ge=0)  # 2 GiB

    tile_size: int = Field(default=256, ge=64, le=1024)
    jpeg_quality: int = Field(default=92, ge=1, le=100)

    verify_ssl: bool = True
    http2: bool = True

    def ensure_directories(self) -> None:
        """Create the output and cache directories if they do not exist."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
