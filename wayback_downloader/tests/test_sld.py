"""Tests for reading an SLD and classifying features by its rules."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from wayback_downloader.exceptions import ExportError
from wayback_downloader.export.kml import KML_NAMESPACE, apply_stylesheet
from wayback_downloader.export.sld import classify, parse_sld

NS = f"{{{KML_NAMESPACE}}}"

# Shaped after the MTA fault stylesheet: four filtered rules plus a catch-all,
# and each rule repeated for a second scale band as GeoServer emits them.
SLD = b"""<?xml version="1.0" encoding="UTF-8"?>
<sld:StyledLayerDescriptor xmlns:sld="http://www.opengis.net/sld"
    xmlns:ogc="http://www.opengis.net/ogc" version="1.0.0">
  <sld:NamedLayer><sld:Name>DRYGEO2</sld:Name><sld:UserStyle>
    <sld:FeatureTypeStyle>
      <sld:Rule>
        <sld:Title>DEPREM YUZEY KIRIGI</sld:Title>
        <ogc:Filter><ogc:PropertyIsEqualTo>
          <ogc:PropertyName>faytipi</ogc:PropertyName><ogc:Literal>1</ogc:Literal>
        </ogc:PropertyIsEqualTo></ogc:Filter>
        <sld:LineSymbolizer><sld:Stroke>
          <sld:CssParameter name="stroke">#ffc321</sld:CssParameter>
          <sld:CssParameter name="stroke-width">3</sld:CssParameter>
        </sld:Stroke></sld:LineSymbolizer>
      </sld:Rule>
      <sld:Rule>
        <sld:Title>HOLOSEN FAYI</sld:Title>
        <ogc:Filter><ogc:PropertyIsEqualTo>
          <ogc:PropertyName>faytipi</ogc:PropertyName><ogc:Literal>2</ogc:Literal>
        </ogc:PropertyIsEqualTo></ogc:Filter>
      </sld:Rule>
      <sld:Rule>
        <sld:Title>DEPREM YUZEY KIRIGI</sld:Title>
        <ogc:Filter><ogc:PropertyIsEqualTo>
          <ogc:PropertyName>faytipi</ogc:PropertyName><ogc:Literal>1</ogc:Literal>
        </ogc:PropertyIsEqualTo></ogc:Filter>
      </sld:Rule>
      <sld:Rule>
        <sld:Title>DIGER FAY HATLARI</sld:Title>
        <sld:LineSymbolizer><sld:Stroke>
          <sld:CssParameter name="stroke">#999999</sld:CssParameter>
        </sld:Stroke></sld:LineSymbolizer>
      </sld:Rule>
    </sld:FeatureTypeStyle>
  </sld:UserStyle></sld:NamedLayer>
</sld:StyledLayerDescriptor>
"""


def test_rules_are_read_in_order() -> None:
    """Order matters: the specific rules precede the catch-all."""
    rules = parse_sld(SLD)
    assert [rule.label for rule in rules] == [
        "DEPREM YUZEY KIRIGI",
        "HOLOSEN FAYI",
        "DIGER FAY HATLARI",
    ]


def test_repeated_rules_are_collapsed() -> None:
    """GeoServer repeats each rule per scale band; they say the same thing."""
    assert len(parse_sld(SLD)) == 3


def test_symbolizer_parameters_are_kept() -> None:
    """The rule's own styling is available even though it is not applied."""
    rules = parse_sld(SLD)
    assert rules[0].css["stroke"] == "#ffc321"
    assert rules[0].css["stroke-width"] == "3"


@pytest.mark.parametrize(
    ("faytipi", "expected"),
    [
        ("1", "DEPREM YUZEY KIRIGI"),
        ("2", "HOLOSEN FAYI"),
        ("9", "DIGER FAY HATLARI"),
    ],
)
def test_features_are_classified_by_their_attributes(faytipi: str, expected: str) -> None:
    """The filters decide, not the colours.

    Colour would be wrong here: on the live layer every placemark renders in
    the catch-all's grey because the rule that selects them defines no stroke,
    so colour-matching would label them all "DIGER FAY HATLARI".
    """
    rule = classify(parse_sld(SLD), {"faytipi": faytipi})
    assert rule is not None
    assert rule.label == expected


def test_a_rule_without_a_filter_catches_everything() -> None:
    """That is how SLD spells a default."""
    rules = parse_sld(SLD)
    assert rules[-1].matches({"anything": "at all"}) is True


def test_numbers_are_compared_as_numbers() -> None:
    """Attributes arrive as text, where "10" sorts before "9"."""
    document = SLD.replace(b"PropertyIsEqualTo", b"PropertyIsGreaterThan")
    rules = parse_sld(document)
    assert rules[0].matches({"faytipi": "10"}) is True
    assert rules[0].matches({"faytipi": "0"}) is False


def test_a_missing_attribute_does_not_match() -> None:
    """A rule filtering on an absent column selects nothing."""
    rules = parse_sld(SLD)
    assert rules[0].matches({"other": "1"}) is False


def test_an_unsupported_filter_leaves_features_unlabelled() -> None:
    """Better to label nothing than to label wrongly.

    An unrecognised construct makes its rule non-matching, so features fall
    through to the catch-all or stay unclassified.
    """
    document = SLD.replace(b"<ogc:PropertyIsEqualTo>", b"<ogc:Intersects>").replace(
        b"</ogc:PropertyIsEqualTo>", b"</ogc:Intersects>"
    )
    rules = parse_sld(document)
    assert rules[0].matches({"faytipi": "1"}) is False


def test_an_empty_stylesheet_is_rejected() -> None:
    """A document with no rules cannot classify anything."""
    with pytest.raises(ExportError, match="no rules"):
        parse_sld(b'<?xml version="1.0"?><StyledLayerDescriptor/>')


def test_malformed_stylesheet_is_reported() -> None:
    """Broken XML names which document was wrong."""
    with pytest.raises(ExportError, match="stylesheet could not be parsed"):
        parse_sld(b"<sld:Style")


def kml_with(*faytipi: str) -> bytes:
    """Build a KML whose placemarks carry the given faytipi attributes."""
    marks = "".join(f"""
      <Placemark id="p{index}">
        <Style><LineStyle><color>4c999999</color></LineStyle></Style>
        <ExtendedData><Data name="faytipi"><value>{value}</value></Data></ExtendedData>
        <LineString><coordinates>26.{index},39.0</coordinates></LineString>
      </Placemark>""" for index, value in enumerate(faytipi))
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="{KML_NAMESPACE}">'
        f"<Document><Folder><name>old</name></Folder>{marks}</Document></kml>"
    ).encode()


def test_placemarks_are_grouped_into_folders_by_rule() -> None:
    """Folders are what a viewer shows as a legend."""
    merged, report = apply_stylesheet(kml_with("1", "2", "2", "9"), SLD)

    assert report.labelled == 4
    assert report.groups == {
        "DEPREM YUZEY KIRIGI": 1,
        "HOLOSEN FAYI": 2,
        "DIGER FAY HATLARI": 1,
    }

    root = ET.fromstring(merged)
    folders = {
        next(c.text for c in folder if c.tag == f"{NS}name"): len(
            [c for c in folder if c.tag == f"{NS}Placemark"]
        )
        for folder in root.iter()
        if folder.tag == f"{NS}Folder"
    }
    assert folders == {"DEPREM YUZEY KIRIGI": 1, "HOLOSEN FAYI": 2, "DIGER FAY HATLARI": 1}


def test_the_rule_name_is_recorded_on_each_placemark() -> None:
    """Readable in a viewer's balloon, and usable by anything reading the file."""
    merged, _ = apply_stylesheet(kml_with("1"), SLD)
    root = ET.fromstring(merged)
    placemark = next(e for e in root.iter() if e.tag == f"{NS}Placemark")
    values = {
        d.get("name"): next(v.text for v in d if v.tag == f"{NS}value")
        for d in next(c for c in placemark if c.tag == f"{NS}ExtendedData")
    }
    assert values["sld_rule"] == "DEPREM YUZEY KIRIGI"
    assert values["faytipi"] == "1"


def test_styling_and_geometry_are_untouched() -> None:
    """Classification adds names; it does not re-render anything."""
    merged, _ = apply_stylesheet(kml_with("1", "2"), SLD)
    text = merged.decode()
    assert text.count("<LineStyle") == 2
    assert text.count("4c999999") == 2
    assert "26.0,39.0" in text and "26.1,39.0" in text


def test_the_documents_previous_empty_folders_are_removed() -> None:
    """Emptied by the regrouping, they would show as phantom legend entries."""
    merged, _ = apply_stylesheet(kml_with("1"), SLD)
    assert b"<name>old</name>" not in merged


def test_placemarks_without_attributes_cannot_be_classified() -> None:
    """Nothing to filter on means the stylesheet cannot help."""
    bare = (
        f'<?xml version="1.0"?><kml xmlns="{KML_NAMESPACE}"><Document>'
        "<Placemark><LineString><coordinates>1,1</coordinates></LineString></Placemark>"
        "</Document></kml>"
    ).encode()
    with pytest.raises(ExportError, match="No placemark matched any rule"):
        apply_stylesheet(bare, SLD)
