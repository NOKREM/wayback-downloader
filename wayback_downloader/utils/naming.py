"""Turning arbitrary labels into safe filenames.

Service-supplied names routinely contain characters a filesystem will not take.
GeoServer layer identifiers are ``namespace:layer``, and on NTFS a colon does
not fail loudly -- it redirects the write into an alternate data stream, so the
tool reports success while the directory holds a zero-byte file and the image
is invisible to every normal tool. Sanitising is therefore not cosmetic.
"""

from __future__ import annotations

import re

# Reserved on Windows regardless of extension.
_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{n}" for n in range(1, 10)),
    *(f"lpt{n}" for n in range(1, 10)),
}

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_stem(name: str, fallback: str = "output") -> str:
    """Reduce a label to characters that are safe in a filename on any platform.

    Keeps ASCII alphanumerics, dot, dash and underscore; collapses every run of
    anything else into a single underscore. Trailing dots and spaces are
    stripped because Windows silently drops them, and reserved device names are
    suffixed so they cannot resolve to a device.
    """
    cleaned = _UNSAFE.sub("_", name.strip()).strip("._-")
    if not cleaned:
        return fallback
    if cleaned.split(".")[0].lower() in _RESERVED:
        cleaned = f"{cleaned}_"
    return cleaned[:120]
