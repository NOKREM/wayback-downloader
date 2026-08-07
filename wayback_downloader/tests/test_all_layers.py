"""Tests for whole-service downloads and the failures they must survive."""

from __future__ import annotations

import httpx
import pytest

from wayback_downloader.cli import _download_each, _selected_layers
from wayback_downloader.config import Settings
from wayback_downloader.exceptions import (
    ImageryUnavailableError,
    NetworkError,
    ServiceRequestError,
    ValidationError,
)
from wayback_downloader.utils.http import AsyncHttpClient

NAMES = ["a", "b", "c"]


def test_all_layers_selects_everything() -> None:
    """The switch expands to every layer the service publishes."""
    assert _selected_layers(NAMES, None, everything=True) == NAMES


def test_a_named_layer_selects_only_it() -> None:
    """Without the switch the named layer is the whole selection."""
    assert _selected_layers(NAMES, "b", everything=False) == ["b"]


def test_both_together_is_refused() -> None:
    """One of the two was certainly a mistake, so neither is assumed."""
    with pytest.raises(ValidationError, match="not both"):
        _selected_layers(NAMES, "b", everything=True)


def test_neither_is_refused() -> None:
    """A download needs to know what to download."""
    with pytest.raises(ValidationError, match="--all-layers"):
        _selected_layers(NAMES, None, everything=False)


def test_all_layers_on_an_empty_service_is_reported() -> None:
    """Nothing to download is an error, not a silent success."""
    with pytest.raises(ImageryUnavailableError, match="no layers"):
        _selected_layers([], None, everything=True)


async def test_one_failure_does_not_lose_the_rest() -> None:
    """A broken layer must not abandon the layers after it.

    Live services make this routine: one AFAD layer has a style referencing a
    column it does not have, and a whole-service run has to step over it.
    """
    attempted: list[str] = []

    async def download(name: str) -> str:
        attempted.append(name)
        if name == "b":
            raise ServiceRequestError("The requested Style can not be used with this layer.")
        return f"{name}.png"

    done, failed = await _download_each(NAMES, download)

    assert attempted == NAMES
    assert [name for name, _ in done] == ["a", "c"]
    assert [name for name, _ in failed] == ["b"]
    assert "Style" in failed[0][1]


async def test_layers_are_processed_in_order_and_once_each() -> None:
    """Sequential by design: dozens of requests against someone else's server."""
    seen: list[str] = []

    async def download(name: str) -> str:
        seen.append(name)
        return name

    done, failed = await _download_each(NAMES, download)
    assert seen == NAMES
    assert len(done) == 3 and not failed


async def test_every_layer_failing_still_returns_cleanly() -> None:
    """The caller reports the tally; it is not an exception on its own."""

    async def download(name: str) -> str:
        raise ServiceRequestError(f"no {name}")

    done, failed = await _download_each(NAMES, download)
    assert done == []
    assert len(failed) == 3


async def test_a_network_failure_is_caught_like_any_other() -> None:
    """A timeout is a domain error, so one slow layer cannot kill the run.

    Before transport errors were mapped, an exhausted ConnectTimeout escaped as
    a raw httpx exception and aborted a whole-service download with a traceback.
    """

    async def download(name: str) -> str:
        if name == "b":
            raise NetworkError("GetMap failed after 4 retries: ConnectTimeout")
        return name

    done, failed = await _download_each(NAMES, download)
    assert [name for name, _ in done] == ["a", "c"]
    assert "ConnectTimeout" in failed[0][1]


async def test_transport_errors_become_domain_errors() -> None:
    """The HTTP client must not let httpx exceptions past the boundary.

    The retry loop ends with `raise last_error`, so whatever it last saw is
    what escapes -- which for a spent timeout was an httpx type that every
    `except WaybackError` in the codebase ignored.
    """
    settings = Settings(max_retries=0, retry_backoff=0.01)
    client = AsyncHttpClient(settings)

    async def fail(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    client._client.get = fail  # type: ignore[method-assign]
    try:
        with pytest.raises(NetworkError) as excinfo:
            await client.get("https://example.org/x", description="the request")
    finally:
        await client.aclose()

    assert "ConnectTimeout" in str(excinfo.value)
    assert "the request" in str(excinfo.value)


def test_network_error_has_its_own_exit_code() -> None:
    """A transport failure is distinguishable from a service rejection."""
    assert NetworkError.exit_code != ServiceRequestError.exit_code
    assert NetworkError.exit_code == 9
