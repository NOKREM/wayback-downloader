"""Tests for filename sanitisation.

The motivating failure: a GeoServer layer is identified as ``mta:DRYGEO2``, and
writing ``wmts_mta:DRYGEO2_12.jpg`` on NTFS does not raise. Windows interprets
the colon as an alternate-data-stream separator, so the image lands in a hidden
stream attached to a zero-byte file called ``wmts_mta``. The download reports
success and the data is invisible to every normal tool.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wayback_downloader.utils.naming import safe_stem


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mta:DRYGEO2", "mta_DRYGEO2"),
        ("wmts_mta:DRYGEO2_12", "wmts_mta_DRYGEO2_12"),
        ("a/b\\c", "a_b_c"),
        ("what?", "what"),
        ("a<b>c|d*e", "a_b_c_d_e"),
        ('quote"here', "quote_here"),
        ("2023-03-15_z18", "2023-03-15_z18"),
        ("already.fine", "already.fine"),
    ],
)
def test_unsafe_characters_are_replaced(raw: str, expected: str) -> None:
    """Every character a filesystem rejects becomes an underscore."""
    assert safe_stem(raw) == expected


def test_result_is_writable_on_this_platform(tmp_path: Path) -> None:
    """The sanitised name really produces one ordinary file.

    Asserting on the string alone would not have caught the original bug, since
    the unsanitised write also "succeeded".
    """
    stem = safe_stem("mta:DRYGEO2")
    path = tmp_path / f"{stem}.jpg"
    path.write_bytes(b"payload")

    children = list(tmp_path.iterdir())
    assert children == [path]
    assert path.stat().st_size == len(b"payload")


def test_runs_of_unsafe_characters_collapse() -> None:
    """A run of bad characters becomes a single separator, not several."""
    assert safe_stem("a:::b") == "a_b"
    assert safe_stem("a   b") == "a_b"


def test_leading_and_trailing_separators_are_trimmed() -> None:
    """Windows silently drops trailing dots and spaces, so strip them first."""
    assert safe_stem("  name.  ") == "name"
    assert safe_stem("__name__") == "name"


def test_empty_input_falls_back() -> None:
    """A label with nothing usable left yields the fallback."""
    assert safe_stem("") == "output"
    assert safe_stem(":::") == "output"
    assert safe_stem("...", fallback="point") == "point"


@pytest.mark.parametrize("device", ["CON", "con", "NUL", "com1", "LPT9"])
def test_reserved_device_names_are_escaped(device: str) -> None:
    """A layer named after a DOS device must not resolve to that device."""
    result = safe_stem(device)
    assert result.lower() not in {"con", "nul", "com1", "lpt9"}
    assert result.startswith(device)


def test_length_is_bounded() -> None:
    """A pathological label cannot blow past filesystem name limits."""
    assert len(safe_stem("x" * 500)) <= 120
