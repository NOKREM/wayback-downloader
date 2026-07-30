"""Tests for CLI entry-point behaviour that needs no network access.

Assertions about *structure* (which commands and options exist) introspect the
underlying Click objects rather than scraping rendered help, because Rich wraps
its output to the terminal width -- an 80-column CI runner splits a long option
name across lines and a substring search on the rendered text fails while the
option is perfectly well defined.

Assertions about *messages* normalise the output first, for the same reason.
"""

from __future__ import annotations

import re

import typer.main
from typer.testing import CliRunner

from wayback_downloader import __version__
from wayback_downloader.cli import app

runner = CliRunner()

LOCATION_COMMANDS = ("download", "versions", "range", "all", "timelapse", "bbox", "batch")
ALL_COMMANDS = LOCATION_COMMANDS + ("endpoints", "cache")


def plain(text: str) -> str:
    """Strip ANSI styling and collapse whitespace, undoing terminal wrapping."""
    return re.sub(r"\s+", " ", re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)).strip()


def command_options(name: str) -> set[str]:
    """Return every option string declared by a command."""
    group = typer.main.get_command(app)
    command = group.commands[name]  # type: ignore[attr-defined]
    return {opt for param in command.params for opt in param.opts}


def test_version_flag_works_on_its_own() -> None:
    """``--version`` must not require a subcommand.

    The group is declared ``invoke_without_command``; without that, Click
    rejects the call for having no subcommand before the callback ever runs and
    the flag reports "Missing command" instead of a version.
    """
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in plain(result.output)
    assert "Missing command" not in plain(result.output)


def test_bare_invocation_shows_help() -> None:
    """No arguments at all prints help and exits as a usage error."""
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Usage" in plain(result.output)


def test_global_options_without_a_command_show_help() -> None:
    """Global options but no command prints help and exits cleanly.

    This path reaches the group callback, unlike the bare invocation above
    which Click short-circuits, so it needs its own exit.
    """
    result = runner.invoke(app, ["--verbose"])
    assert result.exit_code == 0
    assert "Usage" in plain(result.output)


def test_every_command_is_registered() -> None:
    """All commands stay reachable from the top-level group."""
    group = typer.main.get_command(app)
    assert set(ALL_COMMANDS) <= set(group.commands)  # type: ignore[attr-defined]


def test_every_command_has_help_that_renders() -> None:
    """`--help` succeeds for each command."""
    for command in ALL_COMMANDS:
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, command


def test_zoom_range_is_offered_by_every_location_command() -> None:
    """--zoom-range must stay wired into each location-based command."""
    for command in LOCATION_COMMANDS:
        assert "--zoom-range" in command_options(command), command


def test_non_location_commands_have_no_zoom_range() -> None:
    """`endpoints` and `cache` take no coordinate, so no zoom either."""
    for command in ("endpoints", "cache"):
        assert "--zoom-range" not in command_options(command), command


def test_download_exposes_its_documented_options() -> None:
    """The options the README documents for `download` really exist."""
    options = command_options("download")
    for flag in (
        "--lat",
        "--lon",
        "--date",
        "--zoom",
        "--size",
        "--format",
        "--output",
        "--geotiff",
    ):
        assert flag in options, flag


def test_invalid_coordinate_exits_with_the_validation_code() -> None:
    """A bad argument maps to the validation exit code, not a traceback."""
    result = runner.invoke(app, ["download", "--lat", "95", "--lon", "0", "--date", "2022-01-01"])
    assert result.exit_code == 2
    assert "Invalid coordinate" in plain(result.output)


def test_invalid_date_exits_with_the_validation_code() -> None:
    """An unparseable date is reported with the expected format hint."""
    result = runner.invoke(app, ["download", "--lat", "38.8", "--lon", "27.0", "--date", "nope"])
    assert result.exit_code == 2
    assert "YYYY-MM-DD" in plain(result.output)


def test_invalid_zoom_range_exits_with_the_validation_code() -> None:
    """An inverted zoom span is refused before any network call."""
    result = runner.invoke(
        app,
        [
            "download",
            "--lat",
            "38.8",
            "--lon",
            "27.0",
            "--date",
            "2022-01-01",
            "--zoom-range",
            "19-14",
        ],
    )
    assert result.exit_code == 2
    assert "greater than" in plain(result.output)
