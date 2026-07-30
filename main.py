"""Entry point for running the downloader as a script.

python main.py download --lat 38.7992 --lon 26.9723 --date 2022-04-15 --zoom 18 --size 1024
"""

from __future__ import annotations

from wayback_downloader.cli import app

if __name__ == "__main__":
    app()
