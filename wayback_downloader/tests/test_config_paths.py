"""Tests for default output and cache locations.

These paths must never be anchored to the package directory: once the package
is pip-installed that directory is inside ``site-packages``, and neither user
output nor a multi-gigabyte tile cache belongs in an installation tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from wayback_downloader.config import PROJECT_ROOT, Settings, default_cache_dir


def test_output_is_relative_to_the_working_directory() -> None:
    """The default output directory resolves against the CWD, not the package."""
    settings = Settings()
    assert not settings.output_dir.is_absolute()
    assert settings.output_dir == Path("output")


def test_output_follows_the_working_directory(tmp_path: Path, monkeypatch) -> None:
    """Running from a different directory writes output there."""
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    settings.ensure_directories()
    assert (tmp_path / "output").is_dir()


def test_cache_is_not_inside_the_package(monkeypatch) -> None:
    """The cache never lands in the installation tree."""
    monkeypatch.delenv("WAYBACK_CACHE_DIR", raising=False)
    cache = Settings().cache_dir.resolve()
    assert cache.is_absolute()
    assert PROJECT_ROOT.resolve() not in cache.parents
    assert cache != PROJECT_ROOT.resolve()


def test_cache_honours_xdg_cache_home(monkeypatch) -> None:
    """XDG_CACHE_HOME wins, which is how Termux and Linux relocate caches."""
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg-cache")
    assert default_cache_dir() == Path("/tmp/xdg-cache") / "wayback-downloader"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fallback path")
def test_cache_falls_back_to_dot_cache(monkeypatch) -> None:
    """Without XDG_CACHE_HOME the POSIX default is ~/.cache."""
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert default_cache_dir() == Path.home() / ".cache" / "wayback-downloader"


def test_cache_can_be_overridden_by_environment(monkeypatch, tmp_path: Path) -> None:
    """WAYBACK_CACHE_DIR overrides the platform default."""
    monkeypatch.setenv("WAYBACK_CACHE_DIR", str(tmp_path / "custom"))
    assert Settings().cache_dir == tmp_path / "custom"
