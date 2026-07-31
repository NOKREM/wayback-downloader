"""Exception hierarchy for the Wayback downloader.

Every failure mode the CLI reports maps onto exactly one of these types, so the
CLI can translate an exception into an exit code without inspecting messages.
"""

from __future__ import annotations


class WaybackError(Exception):
    """Base class for every error raised by this package."""

    exit_code: int = 1


class ValidationError(WaybackError):
    """User supplied an invalid coordinate, date, zoom level or size."""

    exit_code = 2


class EndpointDiscoveryError(WaybackError):
    """The Wayback service catalog could not be fetched or parsed.

    Raised when the remote configuration document is unreachable, malformed, or
    no longer matches any known schema -- the signal that Esri changed the
    endpoint layout and the discovery module needs a new adapter.
    """

    exit_code = 3


class ImageryUnavailableError(WaybackError):
    """No Wayback release covers the requested location, date or zoom level."""

    exit_code = 4


class TileDownloadError(WaybackError):
    """One or more tiles could not be retrieved after exhausting all retries."""

    exit_code = 5


class RateLimitError(WaybackError):
    """The server signalled that requests are being issued too quickly."""

    exit_code = 6


class ServiceRequestError(WaybackError):
    """A remote service rejected a request and explained why.

    Carries the server's own message rather than a bare status code, because
    OGC services report the actual problem in the response body -- an unknown
    tile matrix, a mismatched grid size -- and a status code alone is not
    actionable.
    """

    exit_code = 8


class ExportError(WaybackError):
    """An output format could not be written, usually a missing optional dependency."""

    exit_code = 7
