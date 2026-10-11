"""DPMD vehicle map: `TrafficState.js` → its literals (docs/sources/dpmd.md).

The payload is JavaScript, not JSON (DPMD-Q2): `name=<literal>;` statements after a leading
`/* {"TimeStamp": …} */` comment, with unquoted object keys, backtick strings and strings
concatenated with page variables (`` `<img src="`+vehicleImageUrl + `842.jpg">` ``). It is read
by a narrow literal reader, never evaluated; a page variable is kept as `${name}` in its string.
Anything outside that grammar is a `PayloadError`.

`recorded` is what the archive keeps (recorder filter `dpmd`): the timestamp, the data hash and
the vehicles in service, without the stop list, the GPX shapes, the duplicated popup header and
hint, and the driver numbers (DPMD-Q8). Parked vehicles (`off: 1`, DPMD-Q5) are dropped.
"""

from __future__ import annotations

import json
import re
from typing import Any, cast

SOURCE = "dpmd"
CHANNEL = "vehicles"

TIMESTAMP = re.compile(r"\s*/\*\s*(\{.*?\})\s*\*/", re.DOTALL)
NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "`": "`", "'": "'", '"': '"', "/": "/"}
KEPT = ("id", "line", "vehicle", "course", "body", "fuel", "color", "hue", "arrow", "coords")
KEPT_STOPS = ("final1", "final2")
DRIVER_LINE = re.compile(r"<br>Řidič:[^<]*")  # DPMD-Q8


class PayloadError(ValueError):
    """The body is not the JavaScript shape the map page reads."""


class _Reader:
    def __init__(self, text: str) -> None:
        self.text = text
        self.at = 0

    def fail(self, what: str) -> PayloadError:
        return PayloadError(f"{what} at offset {self.at}: {self.text[self.at : self.at + 40]!r}")

    def space(self) -> None:
        while self.at < len(self.text) and self.text[self.at].isspace():
            self.at += 1

    def peek(self) -> str:
        self.space()
        return self.text[self.at] if self.at < len(self.text) else ""

    def expect(self, char: str) -> None:
        if self.peek() != char:
            raise self.fail(f"expected {char!r}")
        self.at += 1

    def name(self) -> str:
        self.space()
        match = NAME.match(self.text, self.at)
        if match is None:
            raise self.fail("expected a name")
        self.at = match.end()
        return match.group()

    def value(self) -> Any:
        char = self.peek()
        if char == "[":
            return self.array()
        if char == "{":
            return self.object()
        if char in "`'":
            return self.concatenation()
        match = NUMBER.match(self.text, self.at)
        if match is not None:
            self.at = match.end()
            number = match.group()
            return float(number) if "." in number else int(number)
        word = self.name()
        if word in ("null", "true", "false"):
            return {"null": None, "true": True, "false": False}[word]
        raise self.fail(f"unexpected {word!r}")

    def array(self) -> list[Any]:
        self.expect("[")
        items: list[Any] = []
        while self.peek() != "]":
            items.append(self.value())
            if self.peek() == ",":
                self.at += 1
        self.at += 1
        return items

    def object(self) -> dict[str, Any]:
        self.expect("{")
        fields: dict[str, Any] = {}
        while self.peek() != "}":
            key = self.name()
            self.expect(":")
            fields[key] = self.value()
            if self.peek() == ",":
                self.at += 1
        self.at += 1
        return fields

    def concatenation(self) -> str:
        parts = [self.string()]
        while self.peek() == "+":
            self.at += 1
            if self.peek() in ("`", "'"):
                parts.append(self.string())
            else:
                parts.append("${" + self.name() + "}")
        return "".join(parts)

    def string(self) -> str:
        quote = self.text[self.at]
        self.at += 1
        out: list[str] = []
        while True:
            if self.at >= len(self.text):
                raise self.fail("unterminated string")
            char = self.text[self.at]
            if char == quote:
                self.at += 1
                return "".join(out)
            if char == "\\":
                escaped = self.text[self.at + 1 : self.at + 2]
                if escaped not in ESCAPES:
                    raise self.fail("unknown escape")
                out.append(ESCAPES[escaped])
                self.at += 2
                continue
            if quote == "`" and self.text.startswith("${", self.at):
                raise self.fail("template substitution")
            out.append(char)
            self.at += 1


def read_traffic_state(body: bytes) -> dict[str, Any]:
    """The statements of `TrafficState.js` by name, plus `TimeStamp` from its leading comment."""

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PayloadError(f"not UTF-8: {error}") from error
    header = TIMESTAMP.match(text)
    if header is None:
        raise PayloadError("no leading TimeStamp comment")
    try:
        stamp = cast(object, json.loads(header.group(1)))
    except json.JSONDecodeError as error:
        raise PayloadError(f"TimeStamp comment is not JSON: {error}") from error
    if not isinstance(stamp, dict) or not isinstance(
        cast(dict[str, Any], stamp).get("TimeStamp"), str
    ):
        raise PayloadError("TimeStamp comment without a TimeStamp")
    statements: dict[str, Any] = {"TimeStamp": cast(dict[str, Any], stamp)["TimeStamp"]}
    reader = _Reader(text)
    reader.at = header.end()
    while reader.peek():
        name = reader.name()
        reader.expect("=")
        statements[name] = reader.value()
        reader.expect(";")
    return statements


def recorded(body: bytes) -> tuple[bytes, int, int]:
    """The archived JSON (see the module docstring), vehicles kept and dropped."""

    statements = read_traffic_state(body)
    positions = statements.get("buspositions")
    if not isinstance(positions, list):
        raise PayloadError("buspositions is not a list")
    kept: list[dict[str, Any]] = []
    entries = cast(list[object], positions)
    for entry in entries:
        if not isinstance(entry, dict):
            raise PayloadError("a bus position is not an object")
        bus = cast(dict[str, Any], entry)
        if bus.get("off") == 1:
            continue
        vehicle = {key: bus[key] for key in KEPT + KEPT_STOPS if key in bus}
        if isinstance(vehicle.get("body"), str):
            vehicle["body"] = DRIVER_LINE.sub("", vehicle["body"])
        kept.append(vehicle)
    document = {
        "TimeStamp": statements["TimeStamp"],
        "datahash": statements.get("datahash"),
        "buspositions": kept,
    }
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, len(kept), len(entries) - len(kept)
