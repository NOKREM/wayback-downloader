"""Tests for endpoint discovery and its tolerance of schema changes."""

from __future__ import annotations

import datetime as dt

import pytest

from wayback_downloader.api.discovery import (
    derive_service_base,
    detect_schema_drift,
    parse_release,
)
from wayback_downloader.exceptions import EndpointDiscoveryError
from wayback_downloader.models import WaybackRelease

BASE = (
    "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery"
    "/WMTS/1.0.0/default028mm/MapServer"
)

# A verbatim record from the live configuration document.
LIVE_RECORD = {
    "itemID": "ad62e5488ac441cbaf487ac9268c0590",
    "itemTitle": "World Imagery (Wayback 2026-06-30)",
    "itemURL": f"{BASE}/tile/32246/{{level}}/{{row}}/{{col}}",
    "metadataLayerUrl": (
        "https://metadata.maptiles.arcgis.com/arcgis/rest/services"
        "/World_Imagery_Metadata_2026_r06/MapServer"
    ),
    "metadataLayerItemID": "91d6bfe372d043e69d99b3f478872e96",
    "layerIdentifier": "WB_2026_R06",
}


def test_parses_a_live_record() -> None:
    """Every field of a real configuration record is extracted."""
    release = parse_release("32246", LIVE_RECORD)
    assert release is not None
    assert release.release_num == 32246
    assert release.release_date == dt.date(2026, 6, 30)
    assert release.layer_identifier == "WB_2026_R06"
    assert release.item_id == "ad62e5488ac441cbaf487ac9268c0590"


def test_release_number_is_not_chronological() -> None:
    """Ordering by release number would produce the wrong timeline."""
    older = parse_release(
        "64776", {**LIVE_RECORD, "itemTitle": "World Imagery (Wayback 2023-08-31)"}
    )
    newer = parse_release(
        "64001", {**LIVE_RECORD, "itemTitle": "World Imagery (Wayback 2026-02-26)"}
    )
    assert older is not None and newer is not None
    assert older.release_num > newer.release_num
    assert older.release_date < newer.release_date


def test_accepts_renamed_fields() -> None:
    """A record using alternative key spellings still parses."""
    record = {
        "itemId": "abc123",
        "title": "World Imagery (Wayback 2020-01-15)",
        "urlTemplate": f"{BASE}/tile/999/{{level}}/{{row}}/{{col}}",
    }
    release = parse_release(None, record)
    assert release is not None
    assert release.release_num == 999
    assert release.release_date == dt.date(2020, 1, 15)


def test_normalizes_alternative_placeholders() -> None:
    """A ``{z}/{y}/{x}`` template is rewritten to the canonical form."""
    record = {
        "itemID": "abc",
        "itemTitle": "World Imagery (Wayback 2019-03-01)",
        "itemURL": f"{BASE}/tile/500/{{z}}/{{y}}/{{x}}",
    }
    release = parse_release("500", record)
    assert release is not None
    assert release.tile_url_template.endswith("/tile/500/{level}/{row}/{col}")


def test_malformed_record_is_dropped_not_raised() -> None:
    """An unusable record yields None so one bad entry cannot abort the load."""
    assert parse_release("1", {"itemTitle": "no url here"}) is None
    assert parse_release("2", {"itemURL": f"{BASE}/tile/1/{{level}}/{{row}}/{{col}}"}) is None
    assert parse_release("3", {**LIVE_RECORD, "itemTitle": "World Imagery (undated)"}) is None


def test_service_base_is_derived_by_majority() -> None:
    """One outlier template cannot redirect requests to the wrong host."""
    good = [_release(index, BASE) for index in range(5)]
    rogue = _release(99, "https://evil.example.com/arcgis/rest/services/X/MapServer")
    assert derive_service_base([*good, rogue]) == BASE


def test_service_base_failure_is_explicit() -> None:
    """An unrecognisable template layout raises a discovery error."""
    broken = WaybackRelease(
        release_num=1,
        item_id="x",
        title="World Imagery (Wayback 2020-01-01)",
        tile_url_template="https://example.com/some/other/scheme",
        release_date=dt.date(2020, 1, 1),
    )
    with pytest.raises(EndpointDiscoveryError, match="schema"):
        derive_service_base([broken])


def test_drift_detection_flags_a_missing_field() -> None:
    """A dropped configuration field is reported as drift."""
    stripped = {key: value for key, value in LIVE_RECORD.items() if key != "metadataLayerUrl"}
    release = parse_release("32246", stripped)
    assert release is not None
    warnings = detect_schema_drift({"32246": stripped}, [release])
    assert any("metadata" in warning for warning in warnings)


def test_no_drift_on_the_current_schema() -> None:
    """The live schema produces no drift warnings."""
    release = parse_release("32246", LIVE_RECORD)
    assert release is not None
    assert detect_schema_drift({"32246": LIVE_RECORD}, [release]) == []


def _release(index: int, base: str) -> WaybackRelease:
    """Build a throwaway release pointing at a given service base."""
    date = dt.date(2020, 1, 1) + dt.timedelta(days=index)
    return WaybackRelease(
        release_num=index,
        item_id=f"item{index}",
        title=f"World Imagery (Wayback {date.isoformat()})",
        tile_url_template=f"{base}/tile/{index}/{{level}}/{{row}}/{{col}}",
        release_date=date,
    )
