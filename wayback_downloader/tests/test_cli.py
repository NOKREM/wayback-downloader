"""Tests for CLI entry-point behaviour that needs no network access."""

from __future__ import annotations

from typer.testing import CliRunner

from wayback_downloader import __version__
from wayback_downloader.cli import app

runner = CliRunner()


def test_version_flag_works_on_its_own() -> None:
    """``--version`` must not require a subcommand.

    The group is declared ``invoke_without_command``; without that, Click
    rejects the call for having no subcommand before the callback ever runs and
    the flag reports "Missing command" instead of a version.
    """
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
    assert "Missing command" not in result.output


def test_bare_invocation_shows_help() -> None:
    """No arguments at all prints help and exits as a usage error."""
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Usage" in result.output


def test_global_options_without_a_command_show_help() -> None:
    """Global options but no command prints help and exits cleanly.

    This path reaches the group callback, unlike the bare invocation above
    which Click short-circuits, so it needs its own exit.
    """
    result = runner.invoke(app, ["--verbose"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_help_lists_every_command() -> None:
    """All commands stay reachable from the top-level help."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "download",
        "versions",
        "range",
        "all",
        "timelapse",
        "bbox",
        "batch",
        "endpoints",
        "cache",
    ):
        assert command in result.output


def test_zoom_range_is_offered_by_every_location_command() -> None:
    """--zoom-range must stay wired into each location-based command."""
    for command in ("download", "versions", "range", "all", "timelapse", "bbox", "batch"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, command
        assert "--zoom-range" in result.output, command


def test_invalid_coordinate_exits_with_the_validation_code() -> None:
    """A bad argument maps to the validation exit code, not a traceback."""
    result = runner.invoke(app, ["download", "--lat", "95", "--lon", "0", "--date", "2022-01-01"])
    assert result.exit_code == 2
    assert "Invalid coordinate" in result.output


def test_invalid_date_exits_with_the_validation_code() -> None:
    """An unparseable date is reported with the expected format hint."""
    result = runner.invoke(app, ["download", "--lat", "38.8", "--lon", "27.0", "--date", "nope"])
    assert result.exit_code == 2
    assert "YYYY-MM-DD" in result.output


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
    assert "greater than" in result.output
