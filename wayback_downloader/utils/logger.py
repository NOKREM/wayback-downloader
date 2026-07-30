"""Rich-backed logging and console output."""

from __future__ import annotations

import logging
from typing import Any

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

_THEME = Theme(
    {
        "info": "cyan",
        "success": "bold green",
        "warn": "yellow",
        "error": "bold red",
        "muted": "dim",
        "field": "bold white",
    }
)

console = Console(theme=_THEME, stderr=False)
error_console = Console(theme=_THEME, stderr=True)

_CONFIGURED = False


def configure_logging(verbose: bool = False, quiet: bool = False) -> None:
    """Install the Rich log handler on the package logger.

    Repeated calls are ignored so that library consumers keep control of the
    root logger configuration.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    if quiet:
        level = logging.ERROR
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    handler = RichHandler(
        console=error_console,
        rich_tracebacks=True,
        show_path=verbose,
        show_time=verbose,
        markup=True,
    )
    handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))

    package_logger = logging.getLogger("wayback_downloader")
    package_logger.handlers.clear()
    package_logger.addHandler(handler)
    package_logger.setLevel(level)
    package_logger.propagate = False

    # httpx logs every request at INFO, which drowns out the progress bar.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger below the package root."""
    suffix = name.split(".")[-1]
    return logging.getLogger(f"wayback_downloader.{suffix}")


def print_kv(label: str, value: Any) -> None:
    """Print an aligned ``label: value`` line to the console."""
    console.print(f"  [field]{label:<22}[/field] [muted]{value}[/muted]")
