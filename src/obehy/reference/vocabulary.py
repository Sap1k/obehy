"""The vehicle attribute vocabulary (`src/obehy/data/vehicles/attributes.toml`)."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal, cast

VOCABULARY = Path(__file__).resolve().parents[1] / "data" / "vehicles" / "attributes.toml"
RAW_PREFIX = "raw:"

AttributeType = Literal["bool", "text", "int"]
_NOT_LETTER_OR_DIGIT = re.compile(r"[^0-9A-Za-z]")
_NOT_DIGIT = re.compile(r"[^0-9]")


def _letters_digits(value: str) -> str:
    return _NOT_LETTER_OR_DIGIT.sub("", value).upper()


def _digits(value: str) -> str:
    return _NOT_DIGIT.sub("", value)


_NORMALISE: dict[str, Callable[[str], str]] = {"letters_digits": _letters_digits, "digits": _digits}


@dataclass(frozen=True, slots=True)
class Pairing:
    """When two sources' reports of one journey are one vehicle (`[pairing]`)."""

    distance_m: float
    apart_m: float
    max_fix_gap_s: float
    min_fixes: int
    confirm_s: float
    hold_s: float


@dataclass(frozen=True, slots=True)
class Attribute:
    name: str
    type: AttributeType
    cs: str
    en: str
    icon: str


@dataclass(frozen=True, slots=True)
class Vocabulary:
    version: str
    touch_s: int
    attributes: dict[str, Attribute]
    # Identifier scheme -> how values compare, in the order a report is matched by them.
    identifiers: dict[str, str]
    pairing: Pairing
    # Live statements that clash: the first source listed wins (`precedence` in the file).
    live_sources: tuple[str, ...] = ()
    attribute_sources: dict[str, tuple[str, ...]] | None = None

    def normalise(self, scheme: str, value: str) -> str:
        """An identifier as compared (a plate: letters and digits, upper case)."""

        rule = self.identifiers.get(scheme)
        if rule is None:
            raise ValueError(f"{scheme!r} is not an identifier scheme")
        return _NORMALISE[rule](value)

    def source_rank(self, attribute: str, source: str) -> int:
        """Where a source's live statement about `attribute` ranks; lower wins."""

        order = (self.attribute_sources or {}).get(attribute, self.live_sources)
        return order.index(source) if source in order else len(order)

    def known(self, name: str) -> bool:
        """In the vocabulary, or kept raw (`raw:<source>:<field>`)."""

        return name in self.attributes or (name.startswith(RAW_PREFIX) and name.count(":") >= 2)

    def check(self, name: str, value: str) -> None:
        """Raise if `name` is not in the vocabulary or `value` is not of its type."""

        if not self.known(name):
            raise ValueError(f"vehicle attribute {name!r} is not in the vocabulary")
        attribute = self.attributes.get(name)
        if attribute is None:
            return
        if attribute.type == "bool" and value not in ("true", "false"):
            raise ValueError(f"{name} takes true or false, not {value!r}")
        if attribute.type == "int" and not value.isdigit():
            raise ValueError(f"{name} takes digits, not {value!r}")


@cache
def load_vocabulary(path: Path = VOCABULARY) -> Vocabulary:
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    attributes = cast(dict[str, dict[str, Any]], document["attributes"])
    identifiers = cast(dict[str, str], document["identifiers"])
    for scheme, rule in identifiers.items():
        if rule not in _NORMALISE or scheme not in attributes:
            raise ValueError(f"identifier {scheme} = {rule!r} is not known")
    pairing = cast(dict[str, Any], document["pairing"])
    precedence = cast(dict[str, Any], document.get("precedence", {}))
    per_attribute = cast(dict[str, list[str]], precedence.get("attributes", {}))
    return Vocabulary(
        version=str(document["attributes_version"]),
        touch_s=int(document["touch_s"]),
        live_sources=tuple(str(x) for x in cast(list[str], precedence.get("live_sources", []))),
        attribute_sources={k: tuple(str(x) for x in v) for k, v in per_attribute.items()},
        identifiers={str(k): str(v) for k, v in identifiers.items()},
        pairing=Pairing(
            float(pairing["distance_m"]),
            float(pairing["apart_m"]),
            float(pairing["max_fix_gap_s"]),
            int(pairing["min_fixes"]),
            float(pairing["confirm_s"]),
            float(pairing["hold_s"]),
        ),
        attributes={
            name: Attribute(
                name,
                cast(AttributeType, str(entry["type"])),
                str(entry["cs"]),
                str(entry["en"]),
                str(entry["icon"]),
            )
            for name, entry in attributes.items()
        },
    )


def flag(value: object) -> str | None:
    """A source's yes/no (`1`/`0`, `true`/`false`, a bool) as an attribute value."""

    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip().lower()
    if text in ("1", "true", "ano", "yes"):
        return "true"
    if text in ("0", "false", "ne", "no"):
        return "false"
    return None
