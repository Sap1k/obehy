"""The canonical JSON of `obehy rt compare` (`realtime/compare.py`): one text for one value, so
two implementations that agree write the same bytes.

A record is a JSON object on one line. Rules:

- **Objects**: keys sorted by code point, separators `,` and `:` with no spaces; no hash order
  anywhere (sets become sorted lists).
- **Strings**: as Python's `json.dumps(..., ensure_ascii=False)` escapes them: `\\"`, `\\\\`,
  `\\b \\f \\n \\r \\t`, any other character below U+0020 as `\\u00xx` (lower-case hex); everything
  else, non-ASCII included, as UTF-8.
- **Integers** (and booleans, `null`) as JSON writes them.
- **Floats** are rounded at the edge to a fixed number of decimals chosen by the key they are
  under (in a list: the list's key): `lat`, `lon`, `latitude`, `longitude` 7; metres (a key
  ending in `_m`, `odometer`) 2; anything else (seconds, speeds, bearings) 3. The value is
  formatted with exactly that many decimals, correctly rounded from the binary value (ties to
  even), then trailing zeros and a trailing `.` are stripped and `-0` becomes `0`: `50.0812345678`
  under `lat` is `50.0812346`, `90.0` is `90`, `-0.0001` under `bearing` is `0`. A float field
  holding an integral value therefore prints like an integer. NaN and infinities are errors.
- **Times**: an aware datetime is its UTC instant, `YYYY-MM-DDTHH:MM:SSZ`, with `.ffffff` (six
  digits) before the `Z` only when the microseconds are not zero (`times.utc_text`); a date is
  `YYYY-MM-DD`. Time strings already inside JSON the code writes itself (the fact JSON of
  `model_json.py`, `trip_state_current.calls`) are kept as written: that text is the contract.
- **Dataclasses** become objects of their fields (fields starting with `_` left out) plus
  `"@type"`: the class name. Enums are their value; tuples and lists are lists. An `Observation`
  inside an effect is only its reference: `{"@type": "ObservationRef", "source", "channel",
  "received_at", "sha256", "item"}` (its facts are in its `observation` row).

Pure: no I/O.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, cast

from obehy.realtime.facts import Observation
from obehy.realtime.times import utc_text

COORDINATES = frozenset({"lat", "lon", "latitude", "longitude"})
METRES = frozenset({"odometer"})


def decimals(key: str | None) -> int:
    """How many decimals a float under `key` keeps."""

    if key in COORDINATES:
        return 7
    if key is not None and (key.endswith("_m") or key in METRES):
        return 2
    return 3


def number(value: float, places: int) -> str:
    """A float as canonical text with `places` decimals (see the module docstring)."""

    if not math.isfinite(value):
        raise ValueError(f"{value!r} has no canonical JSON form")
    text = f"{value:.{places}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def plain(value: object) -> Any:
    """`value` as JSON-ready data: dataclasses, enums, times, dates and sets converted; floats
    kept as floats (rounded only when written, by their key)."""

    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Enum):
        return plain(cast(object, value.value))
    if isinstance(value, datetime):
        return utc_text(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Observation):
        return {
            "@type": "ObservationRef",
            "source": value.source,
            "channel": value.channel,
            "received_at": utc_text(value.received_at),
            "sha256": value.raw.sha256,
            "item": value.raw.item,
        }
    if is_dataclass(value) and not isinstance(value, type):
        out: dict[str, Any] = {"@type": type(value).__name__}
        for f in fields(value):
            if not f.name.startswith("_"):
                out[f.name] = plain(getattr(value, f.name))
        return out
    if isinstance(value, Mapping):
        items = cast(Mapping[object, object], value)
        return {_key(k): plain(v) for k, v in items.items()}
    if isinstance(value, list | tuple):
        return [plain(v) for v in cast(list[object] | tuple[object, ...], value)]
    if isinstance(value, set | frozenset):
        members = [plain(v) for v in cast(set[object] | frozenset[object], value)]
        return sorted(members, key=dumps)
    raise TypeError(f"no canonical JSON for {type(value).__name__}")


def _key(value: object) -> str:
    if isinstance(value, str):
        return value
    converted = plain(value)
    return converted if isinstance(converted, str) else dumps(converted)


def dumps(value: Any, key: str | None = None) -> str:
    """The canonical text of JSON-ready data (`plain`); `key`: what the value is under."""

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return number(value, decimals(key))
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        items = cast(Mapping[str, Any], value)
        return (
            "{"
            + ",".join(
                f"{json.dumps(k, ensure_ascii=False)}:{dumps(items[k], k)}" for k in sorted(items)
            )
            + "}"
        )
    if isinstance(value, list | tuple):
        return "[" + ",".join(dumps(v, key) for v in cast(list[Any], value)) + "]"
    raise TypeError(f"not JSON-ready: {type(value).__name__}")


def line(record: Mapping[str, Any]) -> str:
    """One record: its canonical text and a newline."""

    return dumps(plain(record)) + "\n"
