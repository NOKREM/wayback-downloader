"""Reading an SLD stylesheet and deciding which rule a feature falls under.

A styled KML shows you colours but not what they mean: GeoServer writes an
anonymous ``<Style>`` into each placemark and nothing that names the category.
The names live in the SLD, on the rules -- "HOLOSEN FAYI", "KUVATERNER FAYI" --
each guarded by a filter over the feature's own attributes.

Matching on colour would be easier and wrong. On the MTA fault layer every
placemark in one extent renders in ``#999999``, the catch-all rule's grey,
because the rule that actually selects them (``faytipi=4``) defines no stroke
of its own. Colour would label them all "DIGER FAY HATLARI"; evaluating the
filters labels them "OLASI KUVATERNER FAYI VEYA CIZGISELLIK", which is what
they are.

Only the comparison operators that appear in practice are supported. An
unrecognised filter makes its rule non-matching rather than matching
everything, so an unsupported construct leaves features unlabelled instead of
labelling them wrongly.
"""

from __future__ import annotations

import fnmatch
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from wayback_downloader.exceptions import ExportError
from wayback_downloader.utils.logger import get_logger

logger = get_logger(__name__)


def _local(tag: str) -> str:
    """Return an element tag without its namespace."""
    return tag.rsplit("}", 1)[-1]


def _children(parent: ET.Element, name: str) -> list[ET.Element]:
    """Return direct children with the given local name."""
    return [child for child in parent if _local(child.tag) == name]


def _text_of(parent: ET.Element, name: str) -> str | None:
    """Return the text of the first descendant with the given local name."""
    for element in parent.iter():
        if _local(element.tag) == name:
            return (element.text or "").strip()
    return None


def _as_number(value: Any) -> float | None:
    """Interpret a value as a number, or None when it is not one."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _compare(operator: str, left: Any, right: Any) -> bool:
    """Compare two values the way an SLD filter does.

    Numbers are compared numerically when both sides look numeric, because
    attributes arrive as strings and ``"10" < "9"`` is true as text.
    """
    left_number, right_number = _as_number(left), _as_number(right)
    left_value: Any
    right_value: Any
    if left_number is not None and right_number is not None:
        left_value, right_value = left_number, right_number
    else:
        left_value, right_value = str(left).strip(), str(right).strip()

    if operator == "PropertyIsEqualTo":
        return bool(left_value == right_value)
    if operator == "PropertyIsNotEqualTo":
        return bool(left_value != right_value)
    if operator == "PropertyIsLessThan":
        return bool(left_value < right_value)
    if operator == "PropertyIsGreaterThan":
        return bool(left_value > right_value)
    if operator == "PropertyIsLessThanOrEqualTo":
        return bool(left_value <= right_value)
    if operator == "PropertyIsGreaterThanOrEqualTo":
        return bool(left_value >= right_value)
    return False


def _evaluate(node: ET.Element, attributes: dict[str, Any]) -> bool:
    """Evaluate one filter expression against a feature's attributes."""
    name = _local(node.tag)

    if name == "And":
        return all(_evaluate(child, attributes) for child in node)
    if name == "Or":
        return any(_evaluate(child, attributes) for child in node)
    if name == "Not":
        return not all(_evaluate(child, attributes) for child in node)

    if name in {
        "PropertyIsEqualTo",
        "PropertyIsNotEqualTo",
        "PropertyIsLessThan",
        "PropertyIsGreaterThan",
        "PropertyIsLessThanOrEqualTo",
        "PropertyIsGreaterThanOrEqualTo",
    }:
        prop = _text_of(node, "PropertyName")
        literal = _text_of(node, "Literal")
        if prop is None or literal is None or prop not in attributes:
            return False
        return _compare(name, attributes[prop], literal)

    if name == "PropertyIsNull":
        prop = _text_of(node, "PropertyName")
        return prop is not None and not str(attributes.get(prop, "")).strip()

    if name == "PropertyIsLike":
        prop = _text_of(node, "PropertyName")
        literal = _text_of(node, "Literal")
        if prop is None or literal is None or prop not in attributes:
            return False
        wildcard = node.get("wildCard", "*")
        single = node.get("singleChar", "?")
        pattern = literal.replace(wildcard, "*").replace(single, "?")
        return fnmatch.fnmatch(str(attributes[prop]).lower(), pattern.lower())

    if name == "PropertyIsBetween":
        prop = _text_of(node, "PropertyName")
        lower = next((c for c in node if _local(c.tag) == "LowerBoundary"), None)
        upper = next((c for c in node if _local(c.tag) == "UpperBoundary"), None)
        if prop is None or lower is None or upper is None or prop not in attributes:
            return False
        value = _as_number(attributes[prop])
        low = _as_number(_text_of(lower, "Literal"))
        high = _as_number(_text_of(upper, "Literal"))
        if value is None or low is None or high is None:
            return False
        return low <= value <= high

    if name == "Filter":
        return all(_evaluate(child, attributes) for child in node)

    logger.debug("Unsupported SLD filter element %r; treating the rule as non-matching", name)
    return False


@dataclass
class StyleRule:
    """One rule from an SLD: what it is called and when it applies."""

    title: str
    name: str
    filter_node: ET.Element | None
    is_else: bool
    css: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """The human-readable name to show for this rule."""
        return self.title or self.name or "unnamed rule"

    def matches(self, attributes: dict[str, Any]) -> bool:
        """Whether a feature with these attributes falls under this rule.

        A rule with neither a filter nor an ``ElseFilter`` applies to every
        feature, which is how SLD spells a catch-all.
        """
        if self.is_else:
            return False
        if self.filter_node is None:
            return True
        return _evaluate(self.filter_node, attributes)


def parse_sld(document: bytes) -> list[StyleRule]:
    """Extract the rules of an SLD, in the order they are declared.

    Duplicate rules are dropped: GeoServer repeats each one per scale band, and
    for labelling purposes those repeats say the same thing.
    """
    try:
        root = ET.fromstring(document)
    except ET.ParseError as exc:
        raise ExportError(f"The stylesheet could not be parsed: {exc}") from exc

    rules: list[StyleRule] = []
    seen: set[tuple[str, str]] = set()

    for node in (element for element in root.iter() if _local(element.tag) == "Rule"):
        title = (_text_of(node, "Title") or "").strip()
        name = (_text_of(node, "Name") or "").strip()
        filter_node = next((c for c in node if _local(c.tag) == "Filter"), None)
        is_else = bool(_children(node, "ElseFilter"))

        css = {}
        for parameter in node.iter():
            if _local(parameter.tag) in {"CssParameter", "SvgParameter"}:
                key = parameter.get("name")
                if key:
                    css[key] = (parameter.text or "").strip()

        key_pair = (title, name)
        if key_pair in seen:
            continue
        seen.add(key_pair)
        rules.append(StyleRule(title, name, filter_node, is_else, css))

    if not rules:
        raise ExportError("The stylesheet declares no rules.")
    return rules


def kml_colour(rgb: str, opacity: str | None = None) -> str | None:
    """Convert an SLD ``#rrggbb`` and opacity into KML's ``aabbggrr``.

    The two formats reverse the channel order and put alpha first. Checked
    against GeoServer's own output: ``#999999`` at ``stroke-opacity`` 0.3
    becomes ``4c999999``, which is exactly what its KML writer emits.
    """
    text = (rgb or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None

    try:
        alpha = round(float(opacity) * 255) if opacity else 255
    except (TypeError, ValueError):
        alpha = 255
    alpha = max(0, min(255, alpha))

    return f"{alpha:02x}{text[4:6]}{text[2:4]}{text[0:2]}".lower()


@dataclass(frozen=True)
class RuleStyle:
    """The drawing instructions a rule carries, in KML terms."""

    line_colour: str | None = None
    line_width: str | None = None
    fill_colour: str | None = None

    @property
    def is_empty(self) -> bool:
        """Whether the rule specifies nothing to draw with."""
        return not (self.line_colour or self.line_width or self.fill_colour)


def rule_style(rule: StyleRule) -> RuleStyle:
    """Translate a rule's symbolizer parameters into KML styling.

    A rule that defines no stroke of its own returns an empty style, and the
    caller leaves that placemark as the server rendered it rather than
    inventing a colour for it.
    """
    css = rule.css
    return RuleStyle(
        line_colour=(
            kml_colour(css["stroke"], css.get("stroke-opacity")) if css.get("stroke") else None
        ),
        line_width=css.get("stroke-width"),
        fill_colour=kml_colour(css["fill"], css.get("fill-opacity")) if css.get("fill") else None,
    )


def classify(rules: list[StyleRule], attributes: dict[str, Any]) -> StyleRule | None:
    """Return the first rule a feature falls under, else the ElseFilter rule.

    First-match rather than every match: a stylesheet's rules are ordered, and
    the specific ones precede the catch-all that would otherwise claim
    everything.
    """
    for rule in rules:
        if rule.matches(attributes):
            return rule
    return next((rule for rule in rules if rule.is_else), None)
