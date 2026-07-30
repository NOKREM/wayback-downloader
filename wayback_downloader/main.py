"""Package-level entry point, so ``python -m wayback_downloader.main`` also works."""

from __future__ import annotations

from wayback_downloader.cli import app

if __name__ == "__main__":
    app()
